import threading
from multiprocessing import Process

from pynfb.generators import run_eeg_sim
from pynfb.signals import DerivedSignal
from pynfb.lsl_stream import LSLStream, LSL_STREAM_NAMES
from pynfb.protocols import BaselineProtocol, FeedbackProtocol, ThresholdBlinkFeedbackProtocol
from PyQt4 import QtGui, QtCore
from pynfb.io.hdf5 import load_h5py, save_h5py
from pynfb.io.xml import params_to_xml_file
from pynfb.windows import MainWindow
import numpy as np
from datetime import datetime
import os


# helpers
def int_or_none(string):
    return int(string) if len(string) > 0 else None


class Experiment():
    def __init__(self, app, params):
        self.app = app
        self.params = params
        self.main_timer = None
        self.stream = None
        self.thread = None
        self.restart()
        pass

    def update(self):
        """
        Experiment main update action
        :return: None
        """
        # get next chunk
        chunk = self.stream.get_next_chunk() if self.stream is not None else None
        if chunk is not None:
            # update samples counter
            if self.main.player_panel.start.isChecked():
                self.samples_counter += chunk.shape[0]
            # update and collect current samples
            for i, signal in enumerate(self.signals):
                signal.update(chunk)
                self.current_samples[i] = signal.current_sample
            # redraw signals and raw data
            self.main.redraw_signals(self.current_samples, chunk, self.samples_counter)
            # redraw protocols
            self.subject.update_protocol_state(self.current_samples, chunk_size=chunk.shape[0])
            # change protocol if current_protocol_n_samples has been reached
            if self.samples_counter >= self.current_protocol_n_samples:
                self.next_protocol()

    def next_protocol(self):
        """
        Change protocol
        :return: None
        """
        # reset samples counter
        self.samples_counter = 0
        if self.current_protocol_index < len(self.protocols_sequence) - 1:
            # close previous protocol
            self.protocols_sequence[self.current_protocol_index].close_protocol()
            # reset buffer if previous protocol has true value in update_statistics_in_the_end
            if self.protocols_sequence[self.current_protocol_index].update_statistics_in_the_end:
                self.main.signals_buffer *= 0
            # update current protocol index and n_samples
            self.current_protocol_index += 1
            self.current_protocol_n_samples = self.freq * self.protocols_sequence[self.current_protocol_index].duration
            # change protocol widget
            self.subject.change_protocol(self.protocols_sequence[self.current_protocol_index])
        else:
            # action in the end of protocols sequence
            self.current_protocol_n_samples = np.inf
            self.is_finished = True
            self.subject.close()
            # np.save('results/raw', self.main.raw_recorder)
            # np.save('results/signals', self.main.signals_recorder)
            timestamp_str = datetime.strftime(datetime.now(), '%m-%d_%H-%M-%S')
            dir_name = 'results/{}_{}/'.format(self.params['sExperimentName'], timestamp_str)
            os.makedirs(dir_name)
            save_h5py(dir_name + 'raw.h5', self.main.raw_recorder)
            save_h5py(dir_name + 'signals.h5', self.main.signals_recorder)
            params_to_xml_file(self.params, dir_name + 'settings.xml')
            self.stream.save_info(dir_name + 'lsl_stream_info.xml')

    def restart(self):
        if self.main_timer is not None:
            self.main_timer.stop()
        if self.stream is not None:
           self.stream.inlet.__del__()
        if self.thread is not None:
            self.thread.terminate()

        self.is_finished = False

        # current protocol index
        self.current_protocol_index = 0

        # samples counter for protocol sequence
        self.samples_counter = 0

        # run raw
        self.thread = None
        if self.params['sRawDataFilePath'] != '':
            self.params['sStreamName'] = '_raw'
            source_buffer = load_h5py(self.params['sRawDataFilePath']).T
            self.thread = Process(target=run_eeg_sim, args=(),
                                  kwargs={'chunk_size': 0, 'source_buffer': source_buffer,
                                          'name': self.params['sStreamName']})
            self.thread.start()
        if (self.params['sRawDataFilePath'] == '' and self.params['sStreamName'] == '') or self.params['sStreamName'] == '_generator':
            self.params['sStreamName'] = '_generator'
            self.thread = Process(target=run_eeg_sim, args=(),
                                  kwargs={'chunk_size': 0, 'name': self.params['sStreamName']})
            self.thread.start()

        self.stream = LSLStream(name=self.params['sStreamName'])
        self.freq = self.stream.inlet.info().nominal_srate()
        self.n_channels = self.stream.inlet.info().channel_count()

        # signals
        self.signals = [DerivedSignal(bandpass_high=signal['fBandpassHighHz'],
                                      bandpass_low=signal['fBandpassLowHz'],
                                      name=signal['sSignalName'],
                                      n_channels=self.n_channels,
                                      spatial_matrix=(np.loadtxt(signal['SpatialFilterMatrix'])
                                                      if signal['SpatialFilterMatrix'] != ''
                                                      else None))
                        for signal in self.params['vSignals']]
        self.current_samples = np.zeros_like(self.signals)

        # protocols
        self.protocols = []
        for protocol in self.params['vProtocols']:
            if protocol['sFb_type'] == 'Baseline':
                self.protocols.append(BaselineProtocol(self.signals,
                                                       duration=protocol['fDuration'],
                                                       name=protocol['sProtocolName']))
            elif protocol['sFb_type'] == 'Circle':
                self.protocols.append(FeedbackProtocol(self.signals,
                                                       duration=protocol['fDuration'],
                                                       name=protocol['sProtocolName']))
            elif protocol['sFb_type'] == 'ThresholdBlink':
                self.protocols.append(ThresholdBlinkFeedbackProtocol(self.signals,
                                                                     duration=protocol['fDuration'],
                                                                     name=protocol['sProtocolName'],
                                                                     threshold=protocol['fBlinkThreshold'],
                                                                     time_ms=protocol['fBlinkDurationMs']))
            else:
                raise TypeError('Undefined protocol type')

        # protocols sequence
        names = [protocol.name for protocol in self.protocols]
        self.protocols_sequence = []
        for name in self.params['vPSequence']:
            self.protocols_sequence.append(self.protocols[names.index(name)])
        
        # timer
        self.main_timer = QtCore.QTimer(self.app)
        self.main_timer.timeout.connect(self.update)
        self.main_timer.start(1000 * 1. / self.freq)

        # current protocol number of samples ('frequency' * 'protocol duration')
        self.current_protocol_n_samples = self.freq * self.protocols_sequence[self.current_protocol_index].duration

        # experiment number of samples
        experiment_n_samples = sum([self.freq * p.duration for p in self.protocols_sequence])
        
        # windows
        self.main = MainWindow(signals=self.signals,
                               parent=None,
                               experiment=self,
                               current_protocol=self.protocols_sequence[self.current_protocol_index],
                               n_signals=len(self.signals),
                               experiment_n_samples=experiment_n_samples,
                               freq=self.freq)
        self.subject = self.main.subject_window

    def destroy(self):
        if self.thread is not None:
            self.thread.terminate()
        self.main_timer.stop()
        del self.stream
        self.stream = None
        #del self
