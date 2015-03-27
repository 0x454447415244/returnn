import atexit
import numpy
import sys
import thread
import threading
import time
import theano
import pickle
from EngineUtil import assign_dev_data
from Log import log
from Util import hms, progress_bar, terminal_size, hdf5_strings


class TaskThread(threading.Thread):
    def __init__(self, task, network, devices, data, batches, start_batch=0, pad_batches=False):
      """
      :type task: str
      :type network: Network.LayerNetwork
      :type devices: list[Device.Device]
      :type data: Dataset.Dataset
      :type batches: list[EngineBatch.Batch]
      :type start_batch: int
      :type pad_batches: bool
      """
      threading.Thread.__init__(self, name="TaskThread %s" % task)
      self.start_batch = start_batch
      self.pad_batches = pad_batches
      self.devices = devices
      self.network = network
      self.batches = batches
      self.task = task
      self.data = data
      self.daemon = True
      self.elapsed = 0
      self.finalized = False
      self.score = None
      self.batch_idx = None; " :type: int | None "
      self.device_crash_batch = None; " :type: int | None "
      # There is no generic way to see whether Python is exiting.
      # This is our workaround. We check for it in self.run_inner().
      self.stopped = False
      atexit.register(self.stop)
      self.start()

    def stop(self):
      self.stopped = True

    def assign_dev_data(self, device, batches):
      return assign_dev_data(device, self.data, batches, self.network.recurrent, self.pad_batches)

    def allocate_devices(self, start_batch):
      """
      Sets the device data, i.e. the next batches, via self.batches.
      This calls Dataset.load_seqs() to get the data.
      This sets:
        device.data
        device.targets
        device.ctc_targets
        device.tags
        device.index
      :param int start_batch: start batch index, index of self.batches
      :rtype: (list[Device.Device], list[list[EngineBatch.Batch]], int)
      :returns list of used devices, list of batches per device, and number of batches which were handled.
      Number of batches will always be positive, but devices could be empty on skipped seqs.
      """
      devices = []; " :type: list[Device.Device] "
      devices_batches = []; " :type: list[list[EngineBatch.Batch]] "
      batch_idx = start_batch
      for device in self.devices:
        batches = self.batches[batch_idx:batch_idx + device.num_batches]
        success, batch_adv_idx = self.assign_dev_data(device,batches)
        if success:
          devices.append(device)
          devices_batches.append(batches)
        else:
          # We expect that there was a problem with batch_idx + batch_adv_idx - 1.
          assert batch_adv_idx > 0
          print >> log.v3, "Skipping batches %s because some seqs at %i are missing" % \
                           (range(batch_idx, batch_idx + batch_adv_idx),
                            batches[batch_adv_idx - 1].start[0])
        batch_idx += batch_adv_idx
      batch_adv_idx = batch_idx - start_batch
      assert batch_adv_idx > 0
      return devices, devices_batches, batch_adv_idx

    def prepare_device_for_batch(self, device):
      """ :type device: Device.Device """
      pass
    def get_device_prepare_args(self):
      return {"network": self.network, "updater": None}
    def evaluate(self, batches, results, num_frames):
      """
      :param list[list[EngineBatch.Batch]] batches: batches per device
      :param list[list[numpy.ndarray]] results: results per device
      :type num_frames: int
      :returns some score or None
      """
      pass
    def initialize(self):
      pass
    def finalize(self):
      self.finalized = True

    class DeviceBatchRun:
      def __init__(self, parent, batch_idx):
        """
        :type parent: TaskThread
        """
        self.parent = parent
        self.batch_idx = batch_idx
        self.score = None
        self.num_frames = 0
        self.start()

      def finish(self):
        """
        :returns whether everything is fine.
        """
        if not self.alloc_devices:
          # We skipped segments. That's fine.
          return True

        device_results = self.device_collect_results()
        if device_results is None:
          print >> log.v2, "device crashed on batch", self.batch_idx
          self.parent.device_crash_batch = self.batch_idx
          return False
        assert len(device_results) == len(self.alloc_devices) == len(self.devices_batches)

        self.score = self.parent.evaluate(self.devices_batches, device_results, self.num_frames)

        self.print_process()
        return True

      def start(self):
        self.batch_start_time = time.time()
        self.alloc_devices, self.devices_batches, self.batch_adv_idx = \
          self.parent.allocate_devices(start_batch=self.batch_idx)
        assert self.batch_adv_idx > 0
        # Note that alloc_devices could be empty if we skipped seqs.
        if not self.alloc_devices:
          return
        self.device_run()

      def device_run(self):
        batch = self.batch_idx
        for device in self.alloc_devices:
          if self.parent.network.recurrent:
            print >> log.v5, "running", device.data.shape[1], \
                             "sequences (%i nts)" % (device.data.shape[0] * device.data.shape[1]),
          else:
            print >> log.v5, "running", device.data.shape[0], "frames",
          if device.num_batches == 1:
            print >> log.v5, "of batch %i" % batch,
          else:
            print >> log.v5, "of batches %i-%i" % (batch, batch + device.num_batches - 1),
          print >> log.v5, "/", len(self.parent.batches), "on device", device.name
          #if SprintCommunicator.instance is not None:
          #  SprintCommunicator.instance.segments = device.tags #TODO
          self.num_frames += device.data.shape[0] * device.data.shape[1]
          self.parent.prepare_device_for_batch(device)
          device.run(self.parent.task)
          batch += device.num_batches

      def device_collect_results(self):
        device_results = []
        for device in self.alloc_devices:
          try:
            result = device.result()
          except RuntimeError:
            result = None
          if result is None:
            return None
          assert isinstance(result, list)
          assert len(result) >= 1  # The first entry is expected to be the score as a scalar.
          device_results.append(result)
        return device_results

      def device_mem_usage_str(self, devices):
        """
        :type devices: list[Device.Device]
        :rtype: str | None
        """
        if not devices:
          return None
        mem_info = [device.get_memory_info() for device in devices]
        if len(mem_info) == 1 and mem_info[0] is None:
          return None
        mem_usage = [info.used if info else None for info in mem_info]
        s = ["%s MB" % (mem / (1024*1024)) if mem is not None else "unknown" for mem in mem_usage]
        return "/".join(s)

      def print_process(self):
        if not self.parent.interactive and not log.v[5]:
          return
        start_elapsed = time.time() - self.parent.start_time
        run_elapsed = time.time() - self.batch_start_time
        self.parent.run_times.append(run_elapsed)
        if len(self.parent.run_times) * run_elapsed > 60: self.parent.run_times = self.parent.run_times[1:]
        time_domain = len(self.parent.run_times) * sum([d.num_batches for d in self.alloc_devices])
        time_factor = 0.0 if time_domain == 0.0 else float(sum(self.parent.run_times)) / time_domain
        complete = float(self.batch_idx + self.batch_adv_idx) / len(self.parent.batches)
        remaining = hms(int(time_factor * (len(self.parent.batches) - self.batch_idx - self.batch_adv_idx)))
        if log.verbose[5]:
          mem_usage = self.device_mem_usage_str(self.alloc_devices)
          info = [
            "batch %i" % self.batch_idx,
            "score %f" % self.score if self.score is not None else None,
            "elapsed %s" % hms(start_elapsed),
            "exp. remaining %s" % remaining,
            "complete %.02f%%" % (complete * 100),
            "memory %s" % mem_usage if mem_usage else None
          ]
          print >> log.v5, ", ".join(filter(None, info))
        if self.parent.interactive:
          progress_bar(complete, remaining)

    def device_can_run_async(self):
      if len(self.devices) != 1:
        return False
      if self.devices[0].blocking:
        # If we are in the same proc (= blocking), nothing can be async.
        return False
      if self.devices[0].updater is None:
        # If nothing needs to be updated, we can run async.
        return True
      # We can run async iff we do the updates online.
      return self.devices[0].updater.updateOnDevice

    def handle_model_broken(self, broken_info):
      """
      :type broken_info: ModelBrokenError
      """
      collected_info = {"exception_str": str(broken_info), "batches_str": str(broken_info.batches)}
      device = self.devices[0]  # Any device will do.
      try:
        # Assign device data again to dump what we have sent for these batches to the device.
        success, _ = self.assign_dev_data(device, broken_info.batches)
        assert success
        collected_info["dev_data"] = device.data
        collected_info["dev_targets"] = device.targets
        collected_info["dev_index"] = device.index
      except Exception, e:
        print >> log.v3, "Exception when getting device data. %s" % e
      try:
        train_params = device.get_net_train_params()
        collected_info["train_params"] = train_params
      except Exception, e:
        print >> log.v3, "Exception when getting train params. %s" % e
      try:
        dump_file_name = "model_broken_dump.pickle.log"
        f = open(dump_file_name, "w")
        pickle.dump(collected_info, f)
        f.close()
      except Exception, e:
        print >> log.v3, "Exception when writing model broken info dump. %s" % e
      else:
        print >> log.v1, "Model broken debug info dumped into %r." % dump_file_name

    def run(self):
      # Wrap run_inner() for better exception printing.
      # Thread.__bootstrap_inner() ignores sys.excepthook.
      try:
        try:
          self.run_inner()
        except ModelBrokenError, broken:
          print >> log.v1, "%s failed. %s" % (self.name, broken)
          self.handle_model_broken(broken)
          print ""
          # Currently the most sane thing is to abort.
          thread.interrupt_main()
      except IOError, e:  # Such as broken pipe.
        print >> log.v2, "%s. Some device proc crashed unexpectedly. Maybe just SIGINT." % e
        # Just pass on. We have self.finalized == False which indicates the problem.
      except Exception:
        # Catch all standard exceptions.
        # These are not device errors. We should have caught them in the code
        # and we would leave self.finalized == False.
        # Don't catch KeyboardInterrupt here because that will get send by the main thread
        # when it is exiting. It's never by the user because SIGINT will always
        # trigger KeyboardInterrupt in the main thread only.
        try:
          print >> log.v1, "%s failed" % self.name
          sys.excepthook(*sys.exc_info())
          print ""
        finally:
          # Exceptions are fatal. If we can recover, we should handle it in run_inner().
          thread.interrupt_main()

    def run_inner(self):
      self.start_time = time.time()
      for device in self.devices:
        device.prepare(**self.get_device_prepare_args())
      self.initialize()
      terminal_width, _ = terminal_size()
      self.interactive = (log.v[3] and terminal_width >= 0)
      print >> log.v5, "starting task", self.task
      self.run_times = []

      batch_idx = self.start_batch
      canRunAsync = self.device_can_run_async()
      remainingDeviceRun = None; " :type: DeviceBatchRun "

      if canRunAsync:
        print >> log.v5, "Run %s in async mode." % self.name

      while True:
        # Note about the async logic:
        # We start device.run() twice before we do the first device.result() call.
        # That works because the device proc will push the results on the queue
        # and device.result() reads it from there without sending another command.

        self.batch_idx = batch_idx
        if batch_idx < len(self.batches):
          deviceRun = self.DeviceBatchRun(self, batch_idx)
          batch_idx += deviceRun.batch_adv_idx
        else:
          deviceRun = None

        if remainingDeviceRun:  # Set when canRunAsync.
          try:
            if not remainingDeviceRun.finish():
              return
          except Exception:
            if deviceRun:
              # Finish up so that the dev proc protocol is in a sane state.
              # This is only needed in async mode.
              try:
                deviceRun.device_collect_results()
              except Exception, e:
                print >> log.v3, "In exception cleanup, got another exception:", e
                print >> log.v3, "We ignore this and keep handling the original exception."
            raise

        if not deviceRun:  # Finished loop.
          break

        if canRunAsync:
          remainingDeviceRun = deviceRun
        else:
          if not deviceRun.finish():
            # We leave self.finalized == False. That way, the engine can see that the device crashed.
            return

        if self.stopped:
          # This happens when we exit Python.
          # Without this check, this thread would keep running until all exit handlers of Python are done.
          print >> log.v5, "%s stopped" % self
          return

      self.finalize()
      self.elapsed = (time.time() - self.start_time)


class ModelBrokenError(Exception):
  """
  We got a nan/inf at the result somewhere. This means that something is broken.
  """
  def __init__(self, msg, batches):
    assert len(batches) > 0
    super(ModelBrokenError, self).__init__(msg)
    self.batches = batches


class TrainTaskThread(TaskThread):
  def __init__(self, network, devices, data, batches, learning_rate, updater, start_batch, pad_batches):
    """
    :type network: Network.LayerNetwork
    :type devices: list[Device.Device]
    :type data: Dataset.Dataset
    :type batches: list[EngineBatch.Batch]
    :type learning_rate: float
    :type updater: Updater.Updater
    :type start_batch: int
    :type pad_batches: bool
    """
    self.updater = updater
    self.learning_rate = learning_rate
    self.do_ctc_priors = len(network.results) > 1
    self.ctc_priors = None
    # The task is passed to Device.run().
    if self.updater.updateOnDevice:
      task = "train_and_update"
    else:
      task = "train_distributed"
    super(TrainTaskThread, self).__init__(task, network, devices, data, batches, start_batch, pad_batches)

  def initialize(self):
    self.score = 0
    if self.do_ctc_priors:
      self.ctc_priors = numpy.zeros(shape=(self.network.n_out,), dtype=theano.config.floatX)
    if self.updater.updateOnDevice:
      assert len(self.devices) == 1
      self.devices[0].set_learning_rate(self.learning_rate)
    else:
      self.updater.initVars(self.network, None)
      self.updater.setLearningRate(self.learning_rate)
      self.updater_func = self.updater.getUpdateFunction()

  def prepare_device_for_batch(self, device):
    """ :type device: Device.Device """
    device.maybe_update_network(self.network)

  def get_device_prepare_args(self):
    kwargs = super(TrainTaskThread, self).get_device_prepare_args()
    kwargs["updater"] = self.updater
    kwargs["train_param_args"] = self.network.train_param_args
    return kwargs

  def save_ctc_priors(self, filename, epoch_str):
    assert self.ctc_priors is not None
    with open(filename, 'a') as f:
      print >> f, epoch_str
      numpy.savetxt(f, self.ctc_priors, newline=" ")
      print >> f

  def evaluate(self, batches, results, num_frames):
    """
    :param list[list[EngineBatch.Batch]] batches: batches per device
    :param list[(float,params...)] results: result[i] is result for batch + i, result[i][0] is score
    :type num_frames: int
    """
    assert results
    score = sum([res[0] for res in results])
    if numpy.isinf(score) or numpy.isnan(score):
      for i, res in enumerate(results):
        if numpy.isinf(res[0]) or numpy.isnan(res[0]):
          raise ModelBrokenError("Model is broken, got inf or nan score: %s" % score, batches[i])
      assert False  # Should not get here.
    param_start = 1
    if self.do_ctc_priors:
      for res in results:
        self.ctc_priors += res[1]
      param_start = 2
    self.score += score
    if not self.updater.updateOnDevice:
      gparams = {}
      for p in self.network.train_params_vars:
        gparams[p] = numpy.zeros(p.get_value(borrow=True, return_internal_type=True).shape, dtype=theano.config.floatX)
      for res in results:
        for p, q in zip(self.network.train_params_vars, res[param_start:]):
          gparams[p] += q
      self.updater.setNetParamDeltas(gparams)
      self.updater_func()
    return score / num_frames

  def finalize(self):
    if self.updater.updateOnDevice:
      # Copy over params at the very end. Also only if we did training.
      assert len(self.devices) == 1
      params = self.devices[0].get_net_train_params()
      our_params = self.network.train_params_vars
      assert len(params) == len(our_params)
      for i in range(len(params)):
        our_params[i].set_value(params[i])
    if self.data.num_timesteps > 0:
      self.score /= float(self.data.num_timesteps)
      if self.do_ctc_priors:
        self.ctc_priors /= float(self.data.num_timesteps)
    super(TrainTaskThread, self).finalize()


class EvalTaskThread(TaskThread):
    def __init__(self, network, devices, data, batches, start_batch = 0, pad_batches=False):
      super(EvalTaskThread, self).__init__('eval', network, devices, data, batches, start_batch, pad_batches)

    def initialize(self):
      self.score = 0
      self.error = 0
      for device in self.devices:
        device.set_net_params(self.network)

    def evaluate(self, batches, results, num_frames):
      """
      :param list[list[EngineBatch.Batch]] batches: batches per device
      :param list[list[numpy.ndarray]] results: results per device
      :type num_frames: int
      """
      assert results
      score = sum([res[0] for res in results])
      self.score += score
      self.error += sum([res[1] for res in results])
      return score / num_frames

    def finalize(self):
      self.score /= float(self.data.num_timesteps)
      if self.network.loss in ('ctc','ce_ctc'):
        self.error /= float(self.data.num_running_chars)
      else:
        self.error /= float(self.data.num_timesteps)


class SprintCacheForwardTaskThread(TaskThread):
    def __init__(self, network, devices, data, batches, cache, merge = {}, start_batch = 0):
      """
      :type network: Network.LayerNetwork
      :type devices: list[Device.Device]
      :type data: Dataset.Dataset
      :type batches: list[EngineBatch.Batch]
      :type cache: SprintCache.FileArchive
      :type merge: dict
      :type start_batch: int
      """
      super(SprintCacheForwardTaskThread, self).__init__('extract', network, devices, data, batches, start_batch)
      self.cache = cache
      self.merge = merge

    def initialize(self):
      self.toffset = 0

    def evaluate(self, batch, result, num_frames):
      features = numpy.concatenate(result, axis = 1) #reduce(operator.add, device.result())
      if self.merge.keys():
        merged = numpy.zeros((len(features), len(self.merge.keys())), dtype = theano.config.floatX)
        for i in xrange(len(features)):
          for j, label in enumerate(self.merge.keys()):
            for k in self.merge[label]:
              merged[i, j] += numpy.exp(features[i, k])
          z = max(numpy.sum(merged[i]), 0.000001)
          merged[i] = numpy.log(merged[i] / z)
        features = merged
      print >> log.v5, "extracting", len(features[0]), "features over", len(features), "time steps for sequence", self.data.tags[self.data.seq_index[batch]]
      times = zip(range(0, len(features)), range(1, len(features) + 1)) if not self.data.timestamps else self.data.timestamps[self.toffset : self.toffset + len(features)]
      #times = zip(range(0, len(features)), range(1, len(features) + 1))
      self.toffset += len(features)
      self.cache.addFeatureCache(self.data.tags[self.data.seq_index[batch]], numpy.asarray(features), numpy.asarray(times))


class HDFForwardTaskThread(TaskThread):
    def __init__(self, network, devices, data, batches, cache, merge = {}, start_batch = 0):
      super(HDFForwardTaskThread, self).__init__('extract', network, devices, data, batches, start_batch)
      self.tags = []
      self.merge = merge
      self.cache = cache
      cache.attrs['numSeqs'] = data.num_seqs
      cache.attrs['numTimesteps'] = data.num_timesteps
      cache.attrs['inputPattSize'] = data.num_inputs
      cache.attrs['numDims'] = 1
      cache.attrs['numLabels'] = data.num_outputs
      hdf5_strings(cache, 'labels', data.labels)
      self.targets = cache.create_dataset("targetClasses", (data.num_timesteps,), dtype='i')
      self.seq_lengths = cache.create_dataset("seqLengths", (data.num_seqs,), dtype='i')
      self.seq_dims = cache.create_dataset("seqDims", (data.num_seqs, 1), dtype='i')
      if data.timestamps:
        times = cache.create_dataset("times", data.timestamps.shape, dtype='i')
        times[...] = data.timestamps

    def initialize(self):
      self.toffset = 0

    def finalize(self):
      hdf5_strings(self.cache, 'seqTags', self.tags)

    def evaluate(self, batches, results, num_frames):
      features = numpy.concatenate(results, axis=1)
      if not "inputs" in self.cache:
        self.inputs = self.cache.create_dataset("inputs", (self.cache.attrs['numTimesteps'], features.shape[2]), dtype='f')
      if self.merge.keys():
        merged = numpy.zeros((len(features), len(self.merge.keys())), dtype = theano.config.floatX)
        for i in xrange(len(features)):
          for j, label in enumerate(self.merge.keys()):
            for k in self.merge[label]:
              merged[i, j] += numpy.exp(features[i, k])
          z = max(numpy.sum(merged[i]), 0.000001)
          merged[i] = numpy.log(merged[i] / z)
        features = merged
      assert len(batches) == 1
      batch = batches[0]
      assert batch.get_num_seqs() == 1
      seq_idx = batch.start_seq
      print >> log.v5, "extracting", features.shape[2], "features over", features.shape[1], "time steps for sequence", self.data.tags[self.data.seq_index[batches]]
      self.seq_dims[seq_idx] = [features.shape[1]]
      self.seq_lengths[seq_idx] = features.shape[1]
      self.inputs[self.toffset:self.toffset + features.shape[1]] = numpy.asarray(features)
      self.toffset += features.shape[1]
      self.tags.append(self.data.tags[self.data.seq_index[seq_idx]])
