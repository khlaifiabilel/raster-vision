import logging
from os.path import join
import tempfile
import shutil
from typing import TYPE_CHECKING, Optional, List

import numpy as np

from rastervision.pipeline.pipeline import Pipeline
from rastervision.core.box import Box
from rastervision.core.data_sample import DataSample
from rastervision.core.data import Scene, Labels
from rastervision.core.backend import Backend
from rastervision.core.rv_pipeline import TRAIN, VALIDATION
from rastervision.pipeline.file_system.utils import (
    download_if_needed, zipdir, get_local_path, upload_or_copy, make_dir)

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from rastervision.core.rv_pipeline.rv_pipeline_config import RVPipelineConfig  # noqa


class RVPipeline(Pipeline):
    """Base class of all Raster Vision Pipelines.

    This can be subclassed to implement Pipelines for different computer vision tasks
    over geospatial imagery. The commands and what they produce include:
        - analyze: metrics on the imagery and labels
        - chip: small training and validation images taken from larger scenes
        - train: model trained on chips
        - predict: predictions over entire validation and test scenes
        - eval: evaluation metrics for predictions generated by model
        - bundle: bundle containing model and any other files needed to make predictions
            using the Predictor
    """
    commands = ['analyze', 'chip', 'train', 'predict', 'eval', 'bundle']
    split_commands = ['chip', 'predict']
    gpu_commands = ['train', 'predict']

    def __init__(self, config: 'RVPipelineConfig', tmp_dir: str):
        super().__init__(config, tmp_dir)
        self.backend: Optional['Backend'] = None
        self.config: 'RVPipelineConfig'

    def analyze(self):
        """Run each analyzer over training scenes."""
        class_config = self.config.dataset.class_config
        scenes = [
            s.build(class_config, self.tmp_dir, use_transformers=False)
            for s in self.config.dataset.train_scenes
        ]
        analyzers = [a.build() for a in self.config.analyzers]
        for analyzer in analyzers:
            log.info('Running analyzers: {}...'.format(
                type(analyzer).__name__))
            analyzer.process(scenes, self.tmp_dir)

    def get_train_windows(self, scene: Scene) -> List[Box]:
        """Return the training windows for a Scene.

        Each training window represents the spatial extent of a training chip to
        generate.

        Args:
            scene: Scene to generate windows for
        """
        raise NotImplementedError()

    def get_train_labels(self, window: Box, scene: Scene) -> Labels:
        """Return the training labels in a window for a scene.

        Returns:
            Labels that lie within window
        """
        raise NotImplementedError()

    def chip(self, split_ind: int = 0, num_splits: int = 1):
        """Save training and validation chips."""
        cfg = self.config
        backend = cfg.backend.build(cfg, self.tmp_dir)
        dataset = cfg.dataset.get_split_config(split_ind, num_splits)
        if not dataset.train_scenes and not dataset.validation_scenes:
            return

        class_cfg = dataset.class_config
        with backend.get_sample_writer() as writer:

            def chip_scene(scene, split):
                with scene.activate():
                    log.info('Making {} chips for scene: {}'.format(
                        split, scene.id))
                    windows = self.get_train_windows(scene)
                    log.info(f'Writing {len(windows)} chips to disk.')
                    for window in windows:
                        chip = scene.raster_source.get_chip(window)
                        labels = self.get_train_labels(window, scene)
                        sample = DataSample(
                            chip=chip,
                            window=window,
                            labels=labels,
                            scene_id=str(scene.id),
                            is_train=split == TRAIN)
                        sample = self.post_process_sample(sample)
                        writer.write_sample(sample)

            for s in dataset.train_scenes:
                chip_scene(s.build(class_cfg, self.tmp_dir), TRAIN)
            for s in dataset.validation_scenes:
                chip_scene(s.build(class_cfg, self.tmp_dir), VALIDATION)

    def train(self):
        """Train a model and save it."""
        backend = self.config.backend.build(self.config, self.tmp_dir)
        backend.train(source_bundle_uri=self.config.source_bundle_uri)

    def post_process_sample(self, sample: DataSample) -> DataSample:
        """Post-process sample in pipeline-specific way.

        This should be called before writing a sample during chipping.
        """
        return sample

    def post_process_batch(self, windows: List[Box], chips: np.ndarray,
                           labels: Labels) -> Labels:
        """Post-process a batch of predictions."""
        return labels

    def post_process_predictions(self, labels: Labels, scene: Scene) -> Labels:
        """Post-process all labels at end of prediction."""
        return labels

    def get_predict_windows(self, extent: Box) -> List[Box]:
        """Returns windows to compute predictions for.

        Args:
            extent: extent of RasterSource
        """
        chip_sz = stride = self.config.predict_chip_sz
        return extent.get_windows(chip_sz, stride)

    def predict(self, split_ind=0, num_splits=1):
        """Make predictions over each validation and test scene.

        This uses a sliding window.
        """
        # Cache backend so subsquent calls will be faster. This is useful for
        # the predictor.
        if self.backend is None:
            self.backend = self.config.backend.build(self.config, self.tmp_dir)
            self.backend.load_model()

        class_config = self.config.dataset.class_config
        dataset = self.config.dataset.get_split_config(split_ind, num_splits)

        def _predict(scenes):
            for scene in scenes:
                with scene.activate():
                    labels = self.predict_scene(scene, self.backend)
                    label_store = scene.prediction_label_store
                    label_store.save(labels)

        _predict([
            s.build(class_config, self.tmp_dir)
            for s in dataset.validation_scenes
        ])
        if dataset.test_scenes:
            _predict([
                s.build(class_config, self.tmp_dir)
                for s in dataset.test_scenes
            ])

    def predict_scene(self, scene: Scene, backend: Backend) -> Labels:
        """Returns predictions for a single scene."""
        log.info('Making predictions for scene')
        raster_source = scene.raster_source
        label_store = scene.prediction_label_store
        labels = label_store.empty_labels()

        windows = self.get_predict_windows(raster_source.get_extent())

        def predict_batch(chips, windows):
            nonlocal labels
            chips = np.array(chips)
            batch_labels = backend.predict(chips, windows)
            batch_labels = self.post_process_batch(windows, chips,
                                                   batch_labels)
            labels += batch_labels

            print('.' * len(chips), end='', flush=True)

        batch_chips, batch_windows = [], []
        for window in windows:
            chip = raster_source.get_chip(window)
            batch_chips.append(chip)
            batch_windows.append(window)

            # Predict on batch
            if len(batch_chips) >= self.config.predict_batch_sz:
                predict_batch(batch_chips, batch_windows)
                batch_chips, batch_windows = [], []
        print()

        # Predict on remaining batch
        if len(batch_chips) > 0:
            predict_batch(batch_chips, batch_windows)

        return self.post_process_predictions(labels, scene)

    def eval(self):
        """Evaluate predictions against ground truth."""
        class_config = self.config.dataset.class_config
        scenes = [
            s.build(class_config, self.tmp_dir)
            for s in self.config.dataset.validation_scenes
        ]
        evaluators = [e.build(class_config) for e in self.config.evaluators]
        for evaluator in evaluators:
            log.info('Running evaluator: {}...'.format(
                type(evaluator).__name__))
            evaluator.process(scenes, self.tmp_dir)

    def bundle(self):
        """Save a model bundle with whatever is needed to make predictions.

        The model bundle is a zip file and it is used by the Predictor and
        predict CLI subcommand.
        """
        with tempfile.TemporaryDirectory(dir=self.tmp_dir) as tmp_dir:
            bundle_dir = join(tmp_dir, 'bundle')
            make_dir(bundle_dir)

            for fn in self.config.backend.get_bundle_filenames():
                path = download_if_needed(
                    join(self.config.train_uri, fn), tmp_dir)
                shutil.copy(path, join(bundle_dir, fn))

            for a in self.config.analyzers:
                for fn in a.get_bundle_filenames():
                    path = download_if_needed(
                        join(self.config.analyze_uri, fn), tmp_dir)
                    shutil.copy(path, join(bundle_dir, fn))

            path = download_if_needed(self.config.get_config_uri(), tmp_dir)
            shutil.copy(path, join(bundle_dir, 'pipeline-config.json'))

            model_bundle_uri = self.config.get_model_bundle_uri()
            model_bundle_path = get_local_path(model_bundle_uri, self.tmp_dir)
            zipdir(bundle_dir, model_bundle_path)
            upload_or_copy(model_bundle_path, model_bundle_uri)
