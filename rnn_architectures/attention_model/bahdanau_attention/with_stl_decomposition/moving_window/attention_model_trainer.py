import numpy as np
import tensorflow as tf
from tensorflow.python.layers.core import Dense
from tfrecords_handler.moving_window.tfrecord_reader import TFRecordReader
from configs.global_configs import model_training_configs
from configs.global_configs import training_data_configs

class AttentionModelTrainer:

    def __init__(self, **kwargs):
        self.__use_bias = kwargs["use_bias"]
        self.__use_peepholes = kwargs["use_peepholes"]
        self.__input_size = kwargs["input_size"]
        self.__output_size = kwargs["output_size"]
        self.__binary_train_file_path = kwargs["binary_train_file_path"]
        self.__binary_validation_file_path = kwargs["binary_validation_file_path"]
        self.__contain_zero_values = kwargs["contain_zero_values"]
        self.__seed = kwargs["seed"]

    def __l1_loss(self, z, t):
        loss = tf.reduce_mean(tf.abs(t - z))
        return loss

    # Training the time series
    def train_model(self, **kwargs):

        num_hidden_layers = kwargs['num_hidden_layers']
        lstm_cell_dimension = kwargs["lstm_cell_dimension"]
        minibatch_size = kwargs["minibatch_size"]
        max_epoch_size = kwargs["max_epoch_size"]
        max_num_epochs = kwargs["max_num_epochs"]
        l2_regularization = kwargs["l2_regularization"]
        gaussian_noise_stdev = kwargs["gaussian_noise_stdev"]
        random_normal_initializer_stdev = kwargs['random_normal_initializer_stdev']
        optimizer_fn = kwargs["optimizer_fn"]

        tf.reset_default_graph()

        tf.set_random_seed(self.__seed)

        # adding noise to the input
        input = tf.placeholder(dtype=tf.float32, shape=[None, None, self.__input_size])
        noise = tf.random_normal(shape=tf.shape(input), mean=0.0, stddev=gaussian_noise_stdev, dtype=tf.float32)
        training_input = input + noise

        validation_input = input

        target = tf.placeholder(dtype=tf.float32, shape=[None, None, self.__output_size])

        # placeholder for the sequence lengths
        input_sequence_length = tf.placeholder(dtype=tf.int32, shape=[None])
        output_sequence_length = tf.placeholder(dtype=tf.int32, shape=[None])

        weight_initializer = tf.truncated_normal_initializer(stddev=random_normal_initializer_stdev, seed=self.__seed)

        # create the model architecture

        # RNN with the LSTM layer
        def lstm_cell():
            lstm_cell = tf.nn.rnn_cell.LSTMCell(num_units=int(lstm_cell_dimension), use_peepholes=self.__use_peepholes,
                                                initializer=weight_initializer)
            return lstm_cell

        # building the encoder network
        multi_layered_encoder_cell = tf.nn.rnn_cell.MultiRNNCell(
            cells=[lstm_cell() for _ in range(int(num_hidden_layers))])

        with tf.variable_scope('train_encoder_scope') as encoder_train_scope:
            training_encoder_outputs, training_encoder_state = tf.nn.dynamic_rnn(cell=multi_layered_encoder_cell,
                                                                                 inputs=training_input,
                                                                                 sequence_length=input_sequence_length,
                                                                                 dtype=tf.float32)

        with tf.variable_scope(encoder_train_scope, reuse=tf.AUTO_REUSE) as encoder_inference_scope:
            inference_encoder_outputs, inference_encoder_states = tf.nn.dynamic_rnn(cell=multi_layered_encoder_cell,
                                                                                    inputs=validation_input,
                                                                                    sequence_length=input_sequence_length,
                                                                                    dtype=tf.float32)

        # the final projection layer to convert the output to the desired dimension
        dense_layer = Dense(units=1, use_bias=self.__use_bias, kernel_initializer=weight_initializer)

        # decoder cell of the decoder network
        multi_layered_decoder_cell = tf.nn.rnn_cell.MultiRNNCell(
            cells=[lstm_cell() for _ in range(int(num_hidden_layers))])

        # building the decoder network for training
        with tf.variable_scope('decoder_train_scope') as decoder_train_scope:
            # creating an attention layer
            training_attention_mechanism = tf.contrib.seq2seq.BahdanauAttention(num_units=lstm_cell_dimension,
                                                                                memory=training_encoder_outputs,
                                                                                memory_sequence_length=input_sequence_length)
            # using the attention wrapper to wrap the decoding cell
            training_decoder_cell = tf.contrib.seq2seq.AttentionWrapper(cell=multi_layered_decoder_cell,
                                                                        attention_mechanism=training_attention_mechanism,
                                                                        attention_layer_size=lstm_cell_dimension)
            # create the initial state for the decoder
            training_decoder_initial_state = training_decoder_cell.zero_state(batch_size=tf.shape(input)[0],
                                                                              dtype=tf.float32).clone(
                cell_state=training_encoder_state)
            training_helper = tf.contrib.seq2seq.ScheduledOutputTrainingHelper(inputs=target,
                                                                               sequence_length=output_sequence_length,
                                                                               sampling_probability=0.0)
            training_decoder = tf.contrib.seq2seq.BasicDecoder(cell=training_decoder_cell, helper=training_helper,
                                                               initial_state=training_decoder_initial_state,
                                                               output_layer=dense_layer)

            # perform the decoding
            training_decoder_outputs, _, _ = tf.contrib.seq2seq.dynamic_decode(decoder=training_decoder)

        # building the decoder network for inference
        with tf.variable_scope(decoder_train_scope, reuse=tf.AUTO_REUSE) as decoder_inference_scope:
            # creating an attention layer
            inference_attention_mechanism = tf.contrib.seq2seq.BahdanauAttention(num_units=lstm_cell_dimension,
                                                                                 memory=inference_encoder_outputs,
                                                                                 memory_sequence_length=input_sequence_length)
            inference_decoder_cell = tf.contrib.seq2seq.AttentionWrapper(cell=multi_layered_decoder_cell,
                                                                         attention_mechanism=inference_attention_mechanism,
                                                                         attention_layer_size=lstm_cell_dimension)
            # create the initial state for the decoder
            inference_decoder_initial_state = inference_decoder_cell.zero_state(batch_size=tf.shape(input)[0],
                                                                                dtype=tf.float32).clone(
                cell_state=training_encoder_state)
            inference_helper = tf.contrib.seq2seq.ScheduledOutputTrainingHelper(inputs=target,
                                                                                sequence_length=output_sequence_length,
                                                                                sampling_probability=1.0)
            inference_decoder = tf.contrib.seq2seq.BasicDecoder(cell=inference_decoder_cell, helper=inference_helper,
                                                                initial_state=inference_decoder_initial_state,
                                                                output_layer=dense_layer)

            # perform the decoding
            inference_decoder_outputs, _, _ = tf.contrib.seq2seq.dynamic_decode(decoder=inference_decoder)

        # error that should be minimized in the training process
        error = self.__l1_loss(training_decoder_outputs[0], target)

        # l2 regularization of the trainable model parameters
        l2_loss = 0.0
        for var in tf.trainable_variables() :
            l2_loss += tf.nn.l2_loss(var)

        l2_loss = tf.multiply(tf.cast(l2_regularization, dtype=tf.float64), tf.cast(l2_loss, dtype=tf.float64))

        total_loss = tf.cast(error, dtype = tf.float64)+ l2_loss

        # create the optimizer
        optimizer = optimizer_fn(total_loss)

        # create the training and validation datasets from the tfrecord files
        training_dataset = tf.data.TFRecordDataset(filenames = [self.__binary_train_file_path], compression_type = "ZLIB")
        validation_dataset = tf.data.TFRecordDataset(filenames = [self.__binary_validation_file_path], compression_type = "ZLIB")

        # parse the records
        tfrecord_reader = TFRecordReader(input_size=self.__input_size, output_size=self.__output_size)

        # define the expected shapes of data after padding
        train_padded_shapes = ([], [tf.Dimension(None), self.__input_size], [tf.Dimension(None), self.__output_size])
        validation_padded_shapes = ([], [tf.Dimension(None), self.__input_size], [tf.Dimension(None), self.__output_size], [tf.Dimension(None), self.__output_size + 1])

        # preparing the training data
        shuffle_seed = tf.placeholder(dtype=tf.int64, shape=[])
        training_dataset = training_dataset.apply(tf.contrib.data.shuffle_and_repeat(buffer_size=training_data_configs.SHUFFLE_BUFFER_SIZE,
                                                                  count=int(max_epoch_size), seed=shuffle_seed))
        training_dataset = training_dataset.map(tfrecord_reader.train_data_parser)

        padded_training_data_batches = training_dataset.padded_batch(batch_size=minibatch_size,
                                                                     padded_shapes=train_padded_shapes)

        training_data_batch_iterator = padded_training_data_batches.make_initializable_iterator()
        next_training_data_batch = training_data_batch_iterator.get_next()

        # preparing the validation data
        validation_dataset = validation_dataset.map(tfrecord_reader.validation_data_parser)

        # create a single batch from all the validation time series by padding the datasets to make the variable sequence lengths fixed
        padded_validation_dataset = validation_dataset.padded_batch(batch_size=minibatch_size,
                                                                    padded_shapes=validation_padded_shapes)

        # get an iterator to the validation data
        validation_data_iterator = padded_validation_dataset.make_initializable_iterator()

        # access the validation data using the iterator
        next_validation_data_batch = validation_data_iterator.get_next()

        smape_final_list = []

        # setup variable initialization
        init_op = tf.global_variables_initializer()

        with tf.Session() as session :
            session.run(init_op)

            for epoch in range(max_num_epochs):
                smape_epoch_list = []
                print("Epoch->", epoch)

                session.run(training_data_batch_iterator.initializer, feed_dict={shuffle_seed: epoch})

                while True:
                    try:
                        training_data_batch_value = session.run(next_training_data_batch, feed_dict={shuffle_seed: epoch})
                        session.run(optimizer,
                                    feed_dict={input: training_data_batch_value[1],
                                               target: training_data_batch_value[2],
                                               sequence_length: training_data_batch_value[0]
                                               })
                    except tf.errors.OutOfRangeError:
                        break

                if epoch % model_training_configs.INFO_FREQ == 0:
                    session.run(validation_data_iterator.initializer)
                    while True:
                        try:
                            # get the batch of validation inputs
                            validation_data_batch_value = session.run(next_validation_data_batch)

                            # shape for the target data
                            target_data_shape = [np.shape(validation_data_batch_value[1])[0], np.shape(validation_data_batch_value[1])[1], self.__output_size]

                            # get the output of the network for the validation input data batch
                            validation_output = session.run(inference_decoder_outputs[0],
                                                            feed_dict={input: validation_data_batch_value[1],
                                                                       target: np.zeros(target_data_shape),
                                                                       sequence_length: validation_data_batch_value[0]
                                                                       })

                            # calculate the smape for the validation data using vectorization

                            # convert the data to remove the preprocessing
                            last_indices = validation_data_batch_value[0] - 1
                            array_first_dimension = np.array(range(0, validation_data_batch_value[0].shape[0]))

                            true_seasonality_values = validation_data_batch_value[3][array_first_dimension,
                                                      last_indices, 1:]
                            level_values = validation_data_batch_value[3][array_first_dimension, last_indices, 0]

                            last_validation_outputs = validation_output[array_first_dimension, last_indices]
                            converted_validation_output = np.exp(
                                true_seasonality_values + level_values[:, np.newaxis] + last_validation_outputs)

                            actual_values = validation_data_batch_value[2][array_first_dimension, last_indices, :]
                            converted_actual_values = np.exp(
                                true_seasonality_values + level_values[:, np.newaxis] + actual_values)

                            if (self.__contain_zero_values):  # to compensate for 0 values in data
                                converted_validation_output = converted_validation_output - 1
                                converted_actual_values = converted_actual_values - 1

                                # calculate the smape
                            smape = np.mean(np.abs(converted_validation_output - converted_actual_values) /
                                            (np.abs(converted_validation_output) + np.abs(converted_actual_values))) * 2
                            smape_epoch_list.append(smape)

                        except tf.errors.OutOfRangeError:
                            break
                smape_epoch = np.mean(smape_epoch_list)
                smape_final_list.append(smape_epoch)

            smape_final = np.mean(smape_final_list)
            print("SMAPE value: {}".format(smape_final))

        return smape_final



