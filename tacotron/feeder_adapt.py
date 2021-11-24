import os
import threading
import time
import traceback

import numpy as np
import tensorflow as tf
from infolog import log
from tacotron.utils.text import text_to_sequence

_batches_per_group = 64

class Feeder:
	"""
		Feeds batches of data into queue on a background thread.
	"""
	def __init__(self, coordinator, metadata_filename, hparams):
		super(Feeder, self).__init__()
		self._coord = coordinator
		self._hparams = hparams
		self._cleaner_names = [x.strip() for x in hparams.cleaners.split(',')]
		self._train_offset = 0
		self._test_offset = 0

		# Load metadata
		self._mel_dir = os.path.join(os.path.dirname(metadata_filename), 'mels')
		self._linear_dir = os.path.join(os.path.dirname(metadata_filename), 'linear')
		self._spk_embedding_dir = os.path.join(os.path.dirname(metadata_filename), hparams.embedding_path)
		with open(metadata_filename, encoding='utf-8') as f:
			self._metadata = [line.strip().split('|') for line in f]
			frame_shift_ms = hparams.hop_size / hparams.sample_rate
			hours = sum([int(x[4]) for x in self._metadata]) * frame_shift_ms / (3600)
			log('Loaded metadata for {} examples ({:.2f} hours)'.format(len(self._metadata), hours))

		indices = np.arange(len(self._metadata))
		train_indices = indices

		self._train_meta = list(np.array(self._metadata)[train_indices])

		#pad input sequences with the <pad_token> 0 ( _ )
		self._pad = 0
		#explicitely setting the padding to a value that doesn't originally exist in the spectogram
		#to avoid any possible conflicts, without affecting the output range of the model too much
		if hparams.symmetric_mels:
			self._target_pad = -hparams.max_abs_value
		else:
			self._target_pad = 0.
		#Mark finished sequences with 1s
		self._token_pad = 1.

		with tf.device('/cpu:0'):
			# Create placeholders for inputs and targets. Don't specify batch size because we want
			# to be able to feed different batch sizes at eval time.
			self._placeholders = [
			tf.placeholder(tf.int32, shape=(None, None), name='inputs'),
			tf.placeholder(tf.int32, shape=(None, ), name='input_lengths'),
			tf.placeholder(tf.float32, shape=(None, None, hparams.num_mels), name='mel_targets'),
			tf.placeholder(tf.float32, shape=(None, None), name='token_targets'),
			tf.placeholder(tf.float32, shape=(None, None, hparams.num_freq), name='linear_targets'),
			tf.placeholder(tf.int32, shape=(None, ), name='targets_lengths'),
			tf.placeholder(tf.int32, shape=(hparams.tacotron_num_gpus, None), name='split_infos'),
			tf.placeholder(tf.int32, shape=(None, ), name='speaker_id'),
			]
			datatype = [tf.int32, tf.int32, tf.float32, tf.float32, tf.float32, tf.int32, tf.int32, tf.int32]
			if hparams.spk_dependent_embedding:
				self._placeholders += [tf.placeholder(tf.float32, shape=(None, hparams.speaker_dim), name='spk_embeddings')]
				datatype += [tf.float32]

			# Create queue for buffering data
			queue = tf.FIFOQueue(_batches_per_group, datatype, name='input_queue')
			self._enqueue_op = queue.enqueue(self._placeholders)
			if hparams.spk_dependent_embedding:
				self.inputs, self.input_lengths, self.mel_targets, self.token_targets, self.linear_targets, \
					self.targets_lengths, self.split_infos, self.speaker_id, self.spk_embedding = queue.dequeue()
			else:
				self.inputs, self.input_lengths, self.mel_targets, self.token_targets, self.linear_targets, \
					self.targets_lengths, self.split_infos, self.speaker_id = queue.dequeue()
			self.inputs.set_shape(self._placeholders[0].shape)
			self.input_lengths.set_shape(self._placeholders[1].shape)
			self.mel_targets.set_shape(self._placeholders[2].shape)
			self.token_targets.set_shape(self._placeholders[3].shape)
			self.linear_targets.set_shape(self._placeholders[4].shape)
			self.targets_lengths.set_shape(self._placeholders[5].shape)
			self.split_infos.set_shape(self._placeholders[6].shape)
			self.speaker_id.set_shape(self._placeholders[7].shape)
			if hparams.spk_dependent_embedding:
				self.spk_embedding.set_shape(self._placeholders[8].shape)

	def start_threads(self, session):
		self._session = session
		thread = threading.Thread(name='background', target=self._enqueue_next_train_group)
		thread.daemon = True #Thread will close when parent quits
		thread.start()

	def _enqueue_next_train_group(self):
		while not self._coord.should_stop():
			start = time.time()

			# Read a group of examples
			n = self._hparams.tacotron_batch_size
			r = self._hparams.outputs_per_step
			examples = [self._get_next_example() for i in range(n * _batches_per_group)]

			# Bucket examples based on similar output sequence length for efficiency
			examples.sort(key=lambda x: x[-1])
			batches = [examples[i: i+n] for i in range(0, len(examples), n)]
			np.random.shuffle(batches)

			log('\nGenerated {} train batches of size {} in {:.3f} sec'.format(len(batches), n, time.time() - start))
			for batch in batches:
				feed_dict = dict(zip(self._placeholders, self._prepare_batch(batch, r)))
				self._session.run(self._enqueue_op, feed_dict=feed_dict)

	def _get_next_example(self):
		"""Gets a single example (input, mel_target, token_target, linear_target, mel_length) from_ disk
		"""
		if self._train_offset >= len(self._train_meta):
			self._train_offset = 0
			np.random.shuffle(self._train_meta)

		meta = self._train_meta[self._train_offset]
		self._train_offset += 1

		text = meta[6]
		speaker_id = meta[7]

		input_data = np.asarray(text_to_sequence(text, self._cleaner_names), dtype=np.int32)
		mel_target = np.load(os.path.join(self._mel_dir, meta[1]))
		if self._hparams.spk_dependent_embedding:
			spk_embedding = np.load(os.path.join(self._spk_embedding_dir, meta[1]))
		else:
			spk_embedding = None
		#Create parallel sequences containing zeros to represent a non finished sequence
		token_target = np.asarray([0.] * (len(mel_target) - 1))
		if self._hparams.predict_linear:
			linear_target = np.load(os.path.join(self._linear_dir, meta[2]))
		else:
			linear_target = np.zeros([mel_target.shape[0],self._hparams.num_freq])
		return (input_data, mel_target, token_target, linear_target, speaker_id, spk_embedding, len(mel_target))

	def _prepare_batch(self, batches, outputs_per_step):
		assert 0 == len(batches) % self._hparams.tacotron_num_gpus
		size_per_device = int(len(batches) / self._hparams.tacotron_num_gpus)
		np.random.shuffle(batches)

		inputs = None
		mel_targets = None
		token_targets = None
		linear_targets = None
		targets_lengths = None
		split_infos = []

		targets_lengths = np.asarray([x[-1] for x in batches], dtype=np.int32) #Used to mask loss
		input_lengths = np.asarray([len(x[0]) for x in batches], dtype=np.int32)
		speaker_ids = np.asarray([x[4] for x in batches], dtype=np.int32)
		if self._hparams.spk_dependent_embedding:
			spk_embeddings = np.asarray([np.squeeze(x[5]) for x in batches], dtype=np.float32)

		#Produce inputs/targets of variables lengths for different GPUs
		for i in range(self._hparams.tacotron_num_gpus):
			batch = batches[size_per_device * i: size_per_device * (i + 1)]
			input_cur_device, input_max_len = self._prepare_inputs([x[0] for x in batch])
			inputs = np.concatenate((inputs, input_cur_device), axis=1) if inputs is not None else input_cur_device
			mel_target_cur_device, mel_target_max_len = self._prepare_targets([x[1] for x in batch], outputs_per_step)
			mel_targets = np.concatenate(( mel_targets, mel_target_cur_device), axis=1) if mel_targets is not None else mel_target_cur_device

			#Pad sequences with 1 to infer that the sequence is done
			token_target_cur_device, token_target_max_len = self._prepare_token_targets([x[2] for x in batch], outputs_per_step)
			token_targets = np.concatenate((token_targets, token_target_cur_device),axis=1) if token_targets is not None else token_target_cur_device
			linear_targets_cur_device, linear_target_max_len = self._prepare_targets([x[3] for x in batch], outputs_per_step)
			linear_targets = np.concatenate((linear_targets, linear_targets_cur_device), axis=1) if linear_targets is not None else linear_targets_cur_device
			split_infos.append([input_max_len, mel_target_max_len, token_target_max_len, linear_target_max_len])

		split_infos = np.asarray(split_infos, dtype=np.int32)
		if self._hparams.spk_dependent_embedding:
			return (inputs, input_lengths, mel_targets, token_targets, linear_targets, targets_lengths, split_infos, speaker_ids, spk_embeddings)
		else:
			return (inputs, input_lengths, mel_targets, token_targets, linear_targets, targets_lengths, split_infos, speaker_ids)

	def _prepare_inputs(self, inputs):
		max_len = max([len(x) for x in inputs])
		return np.stack([self._pad_input(x, max_len) for x in inputs]), max_len

	def _prepare_targets(self, targets, alignment):
		max_len = max([len(t) for t in targets])
		data_len = self._round_up(max_len, alignment)
		return np.stack([self._pad_target(t, data_len) for t in targets]), data_len

	def _prepare_token_targets(self, targets, alignment):
		max_len = max([len(t) for t in targets]) + 1
		data_len = self._round_up(max_len, alignment)
		return np.stack([self._pad_token_target(t, data_len) for t in targets]), data_len

	def _pad_input(self, x, length):
		return np.pad(x, (0, length - x.shape[0]), mode='constant', constant_values=self._pad)

	def _pad_target(self, t, length):
		return np.pad(t, [(0, length - t.shape[0]), (0, 0)], mode='constant', constant_values=self._target_pad)

	def _pad_token_target(self, t, length):
		return np.pad(t, (0, length - t.shape[0]), mode='constant', constant_values=self._token_pad)

	def _round_up(self, x, multiple):
		remainder = x % multiple
		return x if remainder == 0 else x + multiple - remainder

	def _round_down(self, x, multiple):
		remainder = x % multiple
		return x if remainder == 0 else x - remainder
