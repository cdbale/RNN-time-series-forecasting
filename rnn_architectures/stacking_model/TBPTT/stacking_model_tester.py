import numpy as np
import tensorflow as tf
from tfrecords_handler.moving_window.tfrecord_reader import TFRecordReader
from configs.global_configs import training_data_configs

class StackingModelTester:

    def __init__(self, **kwargs):
        self.__use_bias = kwargs["use_bias"]
        self.__use_peepholes = kwargs["use_peepholes"]
        self.__input_size = kwargs["input_size"]
        self.__output_size = kwargs["output_size"]
        self.__binary_train_file_path = kwargs["binary_train_file_path"]
        self.__binary_test_file_path = kwargs["binary_test_file_path"]

    def __l1_loss(self, z, t):
        loss = tf.reduce_mean(tf.abs(t - z))
        return loss

    def __l2_loss(selfself, z, t):
        loss = tf.losses.mean_squared_error(labels=t, predictions=z)
        return loss

    # Training the time series
    def test_model(self, **kwargs):

        # extract the parameters from the kwargs
        num_hidden_layers = kwargs['num_hidden_layers']
        lstm_cell_dimension = kwargs['lstm_cell_dimension']
        minibatch_size = kwargs['minibatch_size']
        max_epoch_size = kwargs['max_epoch_size']
        max_num_epochs = kwargs['max_num_epochs']
        l2_regularization = kwargs['l2_regularization']
        gaussian_noise_stdev = kwargs['gaussian_noise_stdev']
        optimizer_fn = kwargs['optimizer_fn']
        random_normal_initializer_stdev=kwargs['random_normal_initializer_stdev']
        tbptt_chunk_length = kwargs['tbptt_chunk_length']


        # reset the tensorflow graph
        tf.reset_default_graph()

        tf.set_random_seed(1)

        # declare the input and output placeholders
        input = tf.placeholder(dtype=tf.float32, shape=[None, None, self.__input_size])

        # adding the gassian noise to the input layer
        noise = tf.random_normal(shape=tf.shape(input), mean=0.0, stddev=gaussian_noise_stdev, dtype=tf.float32)
        input = input + noise

        true_output = tf.placeholder(dtype=tf.float32, shape=[None, None, self.__output_size])
        sequence_lengths = tf.placeholder(dtype=tf.int64, shape=[None])

        weight_initializer = tf.truncated_normal_initializer(stddev=random_normal_initializer_stdev, seed=1)

        # create the model architecture

        # RNN with the LSTM layer
        def lstm_cell():
            lstm_cell = tf.nn.rnn_cell.LSTMCell(num_units=int(lstm_cell_dimension), use_peepholes=self.__use_peepholes, initializer=weight_initializer)
            return lstm_cell

        initial_state = tf.placeholder(dtype=tf.float32,
                                               shape=[int(num_hidden_layers), 2, None, int(lstm_cell_dimension)])
        layerwise_initial_state = tf.unstack(initial_state, axis=0)
        initial_state_tuple = tuple(
            [tf.nn.rnn_cell.LSTMStateTuple(layerwise_initial_state[layer][0],
                                           layerwise_initial_state[layer][1])
             for layer in range(int(num_hidden_layers))]
        )

        multi_layered_cell = tf.nn.rnn_cell.MultiRNNCell(cells=[lstm_cell() for _ in range(int(num_hidden_layers))])
        rnn_outputs, states = tf.nn.dynamic_rnn(cell=multi_layered_cell, inputs=input, sequence_length=sequence_lengths, initial_state=initial_state_tuple,
                                                dtype=tf.float32)

        # connect the dense layer to the RNN
        prediction_output = tf.layers.dense(inputs=tf.convert_to_tensor(value=rnn_outputs, dtype=tf.float32),
                                      units=self.__output_size,
                                      use_bias=self.__use_bias, kernel_initializer=weight_initializer)

        # error that should be minimized in the training process
        error = self.__l2_loss(prediction_output, true_output)

        # l2 regularization of the trainable model parameters
        l2_loss = 0.0
        for var in tf.trainable_variables():
            l2_loss += tf.nn.l2_loss(var)

        l2_loss = tf.multiply(tf.cast(l2_regularization, dtype=tf.float64), tf.cast(l2_loss, dtype=tf.float64))

        total_loss = tf.cast(error, dtype=tf.float64) + l2_loss

        # create the adagrad optimizer
        optimizer = optimizer_fn(total_loss)

        # create the Dataset objects for the training and test data
        training_dataset = tf.data.TFRecordDataset(filenames = [self.__binary_train_file_path], compression_type = "ZLIB")
        test_dataset = tf.data.TFRecordDataset([self.__binary_test_file_path], compression_type = "ZLIB")

        # parse the records
        tfrecord_reader = TFRecordReader(self.__input_size, self.__output_size)

        # prepare the training data into batches
        # randomly shuffle the time series within the dataset
        training_dataset.shuffle(buffer_size=training_data_configs.SHUFFLE_BUFFER_SIZE)
        training_dataset = training_dataset.map(tfrecord_reader.validation_data_parser)
        training_dataset.repeat(int(max_epoch_size))

        # create the batches by padding the datasets to make the variable sequence lengths fixed within the individual batches
        padded_training_data_batches = training_dataset.padded_batch(batch_size=int(minibatch_size),
                                                                     padded_shapes=(
                                                                         [], [tf.Dimension(None), self.__input_size],
                                                                         [tf.Dimension(None), self.__output_size],
                                                                         [tf.Dimension(None), self.__output_size + 1]))

        # get an iterator to the batches
        training_data_batch_iterator = padded_training_data_batches.make_initializable_iterator()

        # access each batch using the iterator
        next_training_data_batch = training_data_batch_iterator.get_next()

        # preparing the test data
        test_dataset = test_dataset.map(tfrecord_reader.test_data_parser)

        # create a single batch from all the test time series by padding the datasets to make the variable sequence lengths fixed
        padded_test_input_data = test_dataset.padded_batch(batch_size=int(minibatch_size),
                                                           padded_shapes=([], [tf.Dimension(None), self.__input_size],
                                                                          [tf.Dimension(None), self.__output_size + 1]))

        # get an iterator to the test input data batch
        test_input_iterator = padded_test_input_data.make_one_shot_iterator()

        # access the test input batch using the iterator
        test_input_data_batch = test_input_iterator.get_next()

        # setup variable initialization
        init_op = tf.global_variables_initializer()

        with tf.Session() as session:
            session.run(init_op)

            for epoch in range(int(max_num_epochs)):
                print("Epoch->", epoch)
                session.run(training_data_batch_iterator.initializer)
                while True:
                    try:
                        training_data_batch_value = session.run(next_training_data_batch)

                        current_minibatch_size = np.shape(training_data_batch_value[1])[0]

                        number_of_chunks = np.ceil(np.shape(training_data_batch_value[1])[1] / tbptt_chunk_length)

                        input_arrays = np.array_split(training_data_batch_value[1], indices_or_sections=number_of_chunks, axis=1)
                        output_arrays = np.array_split(training_data_batch_value[2], indices_or_sections=number_of_chunks, axis=1)

                        initial_state_value = np.zeros(
                            shape=(int(num_hidden_layers), 2, current_minibatch_size, int(lstm_cell_dimension)),
                            dtype=np.float32)

                        # loop through the truncated mini batches for each mini-batch
                        for i in range(len(input_arrays)):
                            length_comparison_array = np.greater([(i + 1) * number_of_chunks] * np.shape(training_data_batch_value[1])[0],
                                                                 training_data_batch_value[0])
                            sequence_length_values = np.where(length_comparison_array, 0, number_of_chunks)

                            initial_state_value, _, loss = session.run([states, optimizer, total_loss],
                                                                         feed_dict={input: input_arrays[i],
                                                                                    true_output: output_arrays[i],
                                                                                    initial_state: initial_state_value,
                                                                                    sequence_lengths: sequence_length_values
                                                                                    })

                    except tf.errors.OutOfRangeError:
                        break

            # applying the model to the test data

            list_of_forecasts = []
            while True:
                try:

                    # get the batch of test inputs
                    test_input_batch_value = session.run(test_input_data_batch)
                    current_minibatch_size = np.shape(test_input_batch_value[1])[0]
                    initial_state_value = np.zeros(shape=(int(num_hidden_layers), 2, current_minibatch_size, int(lstm_cell_dimension)), dtype=np.float32)

                    # get the output of the network for the test input data batch
                    test_output = session.run(prediction_output,
                                              feed_dict={input: test_input_batch_value[1],
                                                         sequence_lengths: test_input_batch_value[0],
                                                         initial_state: initial_state_value
                                                         })

                    last_output_index = test_input_batch_value[0] - 1
                    array_first_dimension = np.array(range(0, test_input_batch_value[0].shape[0]))
                    forecasts = test_output[array_first_dimension, last_output_index]
                    list_of_forecasts.extend(forecasts.tolist())

                except tf.errors.OutOfRangeError:
                    break

            session.close()
            return list_of_forecasts