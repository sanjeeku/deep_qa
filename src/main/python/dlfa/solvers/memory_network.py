from typing import Dict
from overrides import overrides

from keras import backend as K
from keras.layers import TimeDistributed, Dropout, merge
from keras.models import Model

from ..data.dataset import Dataset, IndexedDataset, TextDataset  # pylint: disable=unused-import
from ..data.text_instance import TrueFalseInstance
from ..layers.knowledge_selectors import selectors, DotProductKnowledgeSelector, ParameterizedKnowledgeSelector
from ..layers.memory_updaters import updaters
from ..layers.entailment_models import entailment_models, entailment_input_combiners
from .nn_solver import NNSolver
from .pretraining.snli_pretrainer import SnliAttentionPretrainer, SnliEntailmentPretrainer


# TODO(matt): make this class abstract, and make a TrueFalseMemoryNetwork subclass.
class MemoryNetworkSolver(NNSolver):
    '''
    We call this a Memory Network Solver because it has an attention over background knowledge, or
    "memory", similar to a memory network.  This implementation generalizes the architecture of the
    original memory network, though, and can be used to implement several papers in the literature,
    as well as some models that we came up with.

    Our basic architecture is as follows:
        Input: a sentence encoding and a set of background knowledge ("memory") encodings

        current_memory = sentence_encoding
        For each memory layer:
           attention_weights = knowledge_selector(current_memory, background)
           aggregated_background = weighted_sum(attention_weights, background)
           current_memory = memory_updater(current_memory, aggregated_background)
        final_score = entailment_model(aggregated_background, current_memory, sentence_encoding)

    There are thus three main knobs that can be turned (in addition to the number of memory
    layers):
        1. the knowledge_selector
        2. the memory_updater
        3. the entailment_model

    The original memory networks paper used the following:
        1. dot product (our DotProductKnowledgeSelector)
        2. sum
        3. linear classifier on top of current_memory

    The attentive reader in "Teaching Machines to Read and Comprehend", Hermann et al., 2015, used
    the following:
        1. a dense layer with a dot product bias (our ParameterizedKnowledgeSelector)
        2. Dense(K.concat([current_memory, aggregated_background]))
        3. Dense(current_memory)

    Our thought is that we should treat the last step as an entailment problem - does the
    background knowledge entail the input sentence?  Previous work was solving a different problem,
    so they used simpler models "entailment".
    '''

    entailment_choices = ['true_false_mlp']
    entailment_default = entailment_choices[0]
    has_binary_entailment = False
    def __init__(self, **kwargs):
        super(MemoryNetworkSolver, self).__init__(**kwargs)
        self.train_background = kwargs['train_background']
        self.positive_train_background = kwargs['positive_train_background']
        self.negative_train_background = kwargs['negative_train_background']
        self.validation_background = kwargs['validation_background']
        self.test_background = kwargs['test_background']
        self.debug_background = kwargs['debug_background']

        self.knowledge_selector = selectors[kwargs['knowledge_selector']]
        self.hard_memory_selection = kwargs['hard_memory_selection']
        self.knowledge_selector_layers = {}
        self.memory_updater = updaters[kwargs['memory_updater']]
        self.memory_updater_layers = {}
        self.entailment_combiner = entailment_input_combiners[kwargs['entailment_input_combiner']](
                self.embedding_size)
        entailment_args = {
                'num_hidden_layers': kwargs['entailment_num_hidden_layers'],
                'hidden_layer_width': kwargs['entailment_hidden_layer_width'],
                'hidden_layer_activation': kwargs['entailment_hidden_layer_activation'],
                }
        if kwargs['entailment_model'] == 'question_answer_mlp':
            entailment_args['answer_dim'] = self.embedding_size
        self.entailment_model = entailment_models[kwargs['entailment_model']](**entailment_args)
        self.num_memory_layers = kwargs['num_memory_layers']

        self.max_knowledge_length = None

        if kwargs['pretrain_entailment']:
            snli_file = kwargs['snli_file']
            self.pretrainers.append(SnliEntailmentPretrainer(self, snli_file))
        if kwargs['pretrain_attention']:
            snli_file = kwargs['snli_file']
            self.pretrainers.append(SnliAttentionPretrainer(self, snli_file))

    @classmethod
    @overrides
    def update_arg_parser(cls, parser):
        super(MemoryNetworkSolver, cls).update_arg_parser(parser)

        parser.add_argument('--train_background', type=str)
        parser.add_argument('--positive_train_background', type=str)
        parser.add_argument('--negative_train_background', type=str)
        parser.add_argument('--validation_background', type=str)
        parser.add_argument('--test_background', type=str)
        parser.add_argument('--debug_background', type=str)

        parser.add_argument('--knowledge_selector', type=str, default='parameterized',
                            choices=selectors.keys(),
                            help='The kind of knowledge selector to use.  See '
                            'knowledge_selectors.py for details.')
        parser.add_argument('--hard_memory_selection', action='store_true',
                            help='Make hard choices instead of using softmax.')
        parser.add_argument('--memory_updater', type=str, default='dense_concat',
                            choices=updaters.keys(),
                            help='The kind of memory updaters to use.  See memory_updaters.py '
                            'for details.')
        parser.add_argument('--entailment_input_combiner', type=str, default='heuristic_matching',
                            choices=entailment_input_combiners.keys(),
                            help='The kind of entailment input combiner.  See entailment_models.py '
                            'for details.')
        parser.add_argument('--entailment_model', type=str, default=cls.entailment_default,
                            choices=cls.entailment_choices,
                            help='The kind of entailment model to use.  See entailment_models.py '
                            'for details.')
        parser.add_argument('--snli_file', type=str,
                            help='Path to SNLI data, formatted as three-column tsv')
        parser.add_argument('--pretrain_attention', action='store_true',
                            help='Use SNLI data to pretrain the attention model')
        parser.add_argument('--pretrain_entailment', action='store_true',
                            help='Use SNLI data to pretrain the entailment model')
        # TODO(matt): I wish there were a better way to do this...  You really want the entailment
        # model object to specify these arguments, and deal with them, instead of having NNSolver
        # have to know about them...  Not sure how to solve this.
        parser.add_argument('--entailment_num_hidden_layers', type=int, default=1,
                            help='Number of hidden layers in the entailment model')
        parser.add_argument('--entailment_hidden_layer_width', type=int, default=50,
                            help='Width of hidden layers in the entailment model')
        parser.add_argument('--entailment_hidden_layer_activation', type=str, default='relu',
                            help='Activation function for hidden layers in the entailment model')
        parser.add_argument('--num_memory_layers', type=int, default=1,
                            help="Number of memory layers in the network. (default 1)")

    @overrides
    def can_train(self) -> bool:
        has_train_background = (self.train_background is not None) or (
                self.positive_train_background is not None and
                self.negative_train_background is not None)
        has_validation_background = self.validation_background is not None
        has_background = has_train_background and has_validation_background
        return has_background and super(MemoryNetworkSolver, self).can_train()

    @overrides
    def can_test(self) -> bool:
        return self.test_background is not None and super(MemoryNetworkSolver, self).can_test()

    @overrides
    def _instance_type(self):
        return TrueFalseInstance

    @classmethod
    @overrides
    def _get_custom_objects(cls):
        custom_objects = super(MemoryNetworkSolver, cls)._get_custom_objects()
        custom_objects['DotProductKnowledgeSelector'] = DotProductKnowledgeSelector
        custom_objects['ParameterizedKnowledgeSelector'] = ParameterizedKnowledgeSelector
        return custom_objects

    @overrides
    def _get_max_lengths(self) -> Dict[str, int]:
        return {
                'word_sequence_length': self.max_sentence_length,
                'background_sentences': self.max_knowledge_length,
                }

    @overrides
    def _set_max_lengths(self, max_lengths: Dict[str, int]):
        self.max_sentence_length = max_lengths['word_sequence_length']
        self.max_knowledge_length = max_lengths['background_sentences']

    @overrides
    def _set_max_lengths_from_model(self):
        self.max_sentence_length = self.model.get_input_shape_at(0)[0][1]
        # TODO(matt): set the background length too.

    def _get_question_shape(self):
        """
        This is the shape of the input word sequences for a question, not including the batch size.
        """
        return (self.max_sentence_length,)

    def _get_background_shape(self):
        """
        This is the shape of background data (word sequences) associated with a question, not
        including the batch size.
        """
        return (self.max_knowledge_length, self.max_sentence_length)

    def _get_knowledge_axis(self):
        """
        We need to merge and concatenate things in the course of the memory network, and we do it
        in the knowledge_length dimension.  This tells us which axis that dimension is in,
        including the batch_size.

        So, for the true/false memory network, which has background input shape
        (batch_size, knowledge_length, sentence_length), this would be 1.  For the multiple choice
        memory network, which has background input shape
        (batch_size, num_options, knowledge_length, sentence_length), this would be 2.
        """
        # pylint: disable=no-self-use
        return 1

    def _get_merged_background_shape(self):
        """
        This method returns a lambda function, which takes input the shape of the question encoding
        and the knowledge encoding, and returns as output the shape of the merged question and
        background encodings.  This merge just stacks the question encoding on top of the
        background encoding, adding one to the knowledge_length dimension.
        """
        knowledge_axis = self._get_knowledge_axis()
        def merged_shape(input_shapes):
            background_shape = [x for x in input_shapes[1]]
            background_shape[knowledge_axis] += 1
            return tuple(background_shape)
        return merged_shape

    def _get_weighted_average_shape(self):
        """
        Similar to _get_merged_background_shape, this method returns the shape of a function that
        averages over the knowledge axis.  All we have to do is drop the knowledge axis from the
        shape.
        """
        knowledge_axis = self._get_knowledge_axis()
        def merged_shape(input_shapes):
            shape = [x for x in input_shapes[0]]
            shape.pop(knowledge_axis)
            return tuple(shape)
        return merged_shape

    def _get_knowledge_selector(self, layer_num: int):
        """
        Instantiates a KnowledgeSelector layer.  This is an overridable method because some
        subclasses might need to TimeDistribute this, for instance.
        """
        if layer_num not in self.knowledge_selector_layers:
            layer = self.knowledge_selector(name='knowledge_selector_%d' % layer_num,
                                            hard_selection=self.hard_memory_selection)
            self.knowledge_selector_layers[layer_num] = layer
        return self.knowledge_selector_layers[layer_num]

    def _get_memory_updater(self, layer_num: int):
        """
        Instantiates a MemoryUpdater layer.  This is an overridable method because some subclasses
        might need to TimeDistribute this, for instance.
        """
        if layer_num not in self.memory_updater_layers:
            layer = self.memory_updater(encoding_dim=self.embedding_size, name='memory_updater_%d' % layer_num)
            self.memory_updater_layers[layer_num] = layer
        return self.memory_updater_layers[layer_num]

    def _get_entailment_combiner(self):
        """
        Instantiates an EntailmentCombiner layer.  This is an overridable method because some
        subclasses might need to TimeDistribute this, for instance.
        """
        return self.entailment_combiner

    def _get_entailment_output(self, combined_input):
        """
        Gets from the combined entailment input to an output that matches the training labels.
        This is typically done using self.entailment_model.classify(), but could do other things
        also.

        To allow for subclasses to take additional inputs in the entailment model, the return value
        is a tuple of ([additional input layers], output layer).  For instance, this is where
        answer options go, for models that separate the question text from the answer options.
        """
        return [], self.entailment_model.classify(combined_input)

    @overrides
    def _build_model(self):
        # Steps 1 and 2: Convert inputs to sequences of word vectors, for both the question
        # inputs and the knowledge inputs.
        question_input_layer, question_embedding = self._get_embedded_sentence_input(
                input_shape=self._get_question_shape(), name_prefix="sentence")
        knowledge_input_layer, knowledge_embedding = self._get_embedded_sentence_input(
                input_shape=self._get_background_shape(), name_prefix="background")

        # Step 3: Encode the two embedded inputs using the sentence encoder.
        question_encoder = self._get_sentence_encoder()

        # Knowledge encoder will have the same encoder running on a higher order tensor.
        # i.e., question_encoder: (samples, num_words, word_dim) -> (samples, word_dim)
        # and knowledge_encoder: (samples, knowledge_len, num_words, word_dim) ->
        #                       (samples, knowledge_len, word_dim)
        # TimeDistributed generally loops over the second dimension.
        knowledge_encoder = TimeDistributed(question_encoder, name='knowledge_encoder')
        encoded_question = question_encoder(question_embedding)  # (samples, word_dim)
        encoded_knowledge = knowledge_encoder(knowledge_embedding)  # (samples, knowledge_len, word_dim)

        # Step 4: Merge the two encoded representations and pass into the knowledge backed scorer.
        # At each step in the following loop, we take the question encoding, or the output of
        # the previous memory layer, merge it with the knowledge encoding and pass it to the
        # current memory layer.
        current_memory = encoded_question

        knowledge_axis = self._get_knowledge_axis()
        for i in range(self.num_memory_layers):
            # We want to merge a matrix and a tensor such that the new tensor will have one
            # additional row (at the beginning) in all slices.
            # (samples, word_dim) + (samples, knowledge_len, word_dim)
            #       -> (samples, 1 + knowledge_len, word_dim)
            # Since this is an unconventional merge, we define a customized lambda merge.
            # Keras cannot infer the shape of the output of a lambda function, so we make
            # that explicit.
            merge_mode = lambda layer_outs: K.concatenate([K.expand_dims(layer_outs[0], dim=knowledge_axis),
                                                           layer_outs[1]],
                                                          axis=knowledge_axis)
            merged_shape = self._get_merged_background_shape()
            merged_encoded_rep = merge([current_memory, encoded_knowledge],
                                       mode=merge_mode,
                                       output_shape=merged_shape,
                                       name='concat_question_with_background_%d' % i)

            # Regularize it
            regularized_merged_rep = Dropout(0.2)(merged_encoded_rep)
            knowledge_selector = self._get_knowledge_selector(i)
            attention_weights = knowledge_selector(regularized_merged_rep)
            # Defining weighted average as a custom merge mode. Takes two inputs: data and weights
            # ndim of weights is one less than data.
            weighted_average = lambda avg_inputs: K.sum(avg_inputs[0] * K.expand_dims(avg_inputs[1], dim=-1),
                                                        axis=knowledge_axis)
            # input shapes: (samples, knowledge_len, input_dim), (samples, knowledge_len)
            # output shape: (samples, input_dim)
            weighted_average_shape = self._get_weighted_average_shape()
            attended_knowledge = merge([encoded_knowledge, attention_weights],
                                       mode=weighted_average,
                                       output_shape=weighted_average_shape,
                                       name='background_weighted_average_%d' % i)

            # To make this easier to TimeDistribute, we're going to concatenate the current memory
            # with the attended knowledge, and use that as the input to the memory updater, instead
            # of just passing a list.
            # We going from two inputs of (batch_size, encoding_dim) to one input of (batch_size,
            # encoding_dim * 2).
            updater_input = merge([current_memory, attended_knowledge],
                                  mode='concat',
                                  concat_axis=knowledge_axis,
                                  name='concat_current_memory_with_background_%d' % i)
            memory_updater = self._get_memory_updater(i)
            current_memory = memory_updater(updater_input)


        # TODO(matt): we now have subclasses that do answer selection, instead of entailment, and
        # it's not very nice to shoehorn them into the same "entailment" model.  It would be nice
        # to generalize this into some "final output" section, but I'm not sure how to do that
        # cleanly.

        # Step 5: Finally, run the sentence encoding, the current memory, and the attended
        # background knowledge through an entailment model to get a final true/false score.
        entailment_input = merge([encoded_question, current_memory, attended_knowledge],
                                 mode='concat',
                                 concat_axis=knowledge_axis,
                                 name='concat_entailment_inputs')
        combined_input = self._get_entailment_combiner()(entailment_input)
        extra_entailment_inputs, entailment_output = self._get_entailment_output(combined_input)

        # Step 6: Define the model, and return it. The model will be compiled and trained by the
        # calling method.
        input_layers = [question_input_layer, knowledge_input_layer]
        input_layers.extend(extra_entailment_inputs)
        memory_network = Model(input=input_layers, output=entailment_output)
        return memory_network

    @overrides
    def _get_training_data(self):
        instance_type = self._instance_type()
        if self.train_file:
            dataset = TextDataset.read_from_file(self.train_file, instance_type, tokenizer=self.tokenizer)
            background_dataset = TextDataset.read_background_from_file(dataset, self.train_background)
        else:
            positive_dataset = TextDataset.read_from_file(self.positive_train_file,
                                                          instance_type,
                                                          label=True,
                                                          tokenizer=self.tokenizer)
            positive_background = TextDataset.read_background_from_file(positive_dataset,
                                                                        self.positive_train_background)
            negative_dataset = TextDataset.read_from_file(self.negative_train_file,
                                                          instance_type,
                                                          label=False,
                                                          tokenizer=self.tokenizer)
            negative_background = TextDataset.read_background_from_file(negative_dataset,
                                                                        self.negative_train_background)
            background_dataset = positive_background.merge(negative_background)
        if self.max_training_instances is not None:
            background_dataset = background_dataset.truncate(self.max_training_instances)
        self.data_indexer.fit_word_dictionary(background_dataset)
        self.training_dataset = background_dataset
        return self.prep_labeled_data(background_dataset, for_train=True, shuffle=True)

    @overrides
    def _get_validation_data(self):
        dataset = TextDataset.read_from_file(self.validation_file, self._instance_type(), tokenizer=self.tokenizer)
        background_dataset = TextDataset.read_background_from_file(dataset, self.validation_background)
        self.validation_dataset = background_dataset
        return self._prep_question_dataset(background_dataset)

    @overrides
    def _get_test_data(self):
        dataset = TextDataset.read_from_file(self.test_file, self._instance_type(), tokenizer=self.tokenizer)
        background_dataset = TextDataset.read_background_from_file(dataset, self.test_background)
        return self._prep_question_dataset(background_dataset)

    @overrides
    def _get_debug_dataset_and_input(self):
        dataset = TextDataset.read_from_file(self.debug_file, self._instance_type(), tokenizer=self.tokenizer)
        background_dataset = TextDataset.read_background_from_file(dataset, self.debug_background)
        # Now get inputs, and ignore the labels (background_dataset has them)
        inputs, _ = self.prep_labeled_data(background_dataset, for_train=False, shuffle=False)
        return background_dataset, inputs

    def get_debug_layer_names(self):
        debug_layer_names = []
        for layer in self.model.layers:
            if "knowledge_selector" in layer.name:
                debug_layer_names.append(layer.name)
        return debug_layer_names

    def debug(self, debug_dataset, debug_inputs, epoch: int):
        """
        A debug_model must be defined by now. Run it on debug data and print the
        appropriate information to the debug output.
        """
        debug_output_file = open("%s_debug_%d.txt" % (self.model_prefix, epoch), "w")
        scores = self.score(debug_inputs)
        attention_outputs = self.debug_model.predict(debug_inputs)
        if self.num_memory_layers == 1:
            attention_outputs = [attention_outputs]
        # Collect values from all hops of attention for a given instance into attention_values.
        for instance, score, *attention_values in zip(debug_dataset.instances,
                                                      scores, *attention_outputs):
            sentence = instance.text
            background_info = instance.background
            label = instance.label
            positive_score = score[1]  # Only get p(t|x)
            # Remove the attention values for padding
            attention_values = [values[-len(background_info):] for values in attention_values]
            print("Sentence: %s" % sentence, file=debug_output_file)
            print("Label: %s" % label, file=debug_output_file)
            print("Assigned score: %.4f" % positive_score, file=debug_output_file)
            print("Weights on background:", file=debug_output_file)
            for i, background_i in enumerate(background_info):
                if i >= len(attention_values[0]):
                    # This happens when IndexedBackgroundInstance.pad() ignored some
                    # sentences (at the end). Let's ignore them too.
                    break
                all_hops_attention_i = ["%.4f" % values[i] for values in attention_values]
                print("\t%s\t%s" % (" ".join(all_hops_attention_i), background_i),
                      file=debug_output_file)
            print(file=debug_output_file)
        debug_output_file.close()
