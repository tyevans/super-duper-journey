from __future__ import division

import os
import queue
import time
import uuid
from multiprocessing import Process, Queue

import cv2
import numpy as np
from django import db
from django.core.management.base import BaseCommand

from objectdash.detect.annotation import Mask
from objectdash.detect.object_detector import ObjectDetector

db.connections.close_all()


class AnnotationProcessor(Process):

    def __init__(self, graph_file: str, label_file: str, num_classes: int, frame_queue: Queue, annotation_queue: Queue):
        """ Dedicated image annotating process.  Sits off to the side

        Pump `None` into the frame queue to exit the process

        :param graph_file: (string) Path to the object detection model's frozen_inference_graph.pb
        :param label_file: (string) Path to the object detection model's labels.pbtxt
        :param num_classes: (int) Number of classes the model is trained on (this is 90 for a coco trained model)
        :param frame_queue: (multiprocessing.Queue) queue where raw images will be provided
        :param annotation_queue: (multiprocessing.Queue) queue where annotations will be returned
        """
        super(AnnotationProcessor, self).__init__()
        self.graph_file = graph_file
        self.label_file = label_file
        self.num_classes = num_classes
        self.frame_queue = frame_queue
        self.annotation_queue = annotation_queue

    def run(self):
        # Create our object detector
        detector = ObjectDetector(self.graph_file, self.label_file, self.num_classes)

        while True:
            # Get the next available frame
            frame = self.frame_queue.get()
            if frame is None:
                break
            # Annotate it
            annotations = detector.annotate(frame)
            # Pump it into the output queue
            self.annotation_queue.put(annotations)


class ImageHandler(object):
    """ Base class for a process that transforms an image

    Used to build pipelines for processing images
    """

    def apply_first(self, image_np):
        """
        Called on the first image/frame when a pipeline is started

        :param image_np: (np.array) cv2 image array (gbr)
        :return: (np.array) transformed image
        """
        return image_np

    def apply(self, image_np):
        """
        Called on all subsequent frames

        :param image_np: (np.array) cv2 image array (gbr)
        :return: (np.array) transformed image
        """
        return image_np

    def close(self):
        """
        Called when processing is complete
        """
        pass


class BackgroundSubtractor(ImageHandler):

    def __init__(self):
        """ Removes background (stationary) elements from an image, accumulating information across multiple images.
        """
        self.annotations = None
        self.fgbg = cv2.createBackgroundSubtractorMOG2()
        self.kernel = np.ones((5, 5), np.uint8)

    def apply_first(self, image_np):
        return self.apply(image_np)

    def apply(self, image_np):
        self.fgbg.apply(image_np)
        motion_mask = self.fgbg.apply(image_np)

        motion_mask = cv2.morphologyEx(motion_mask, cv2.MORPH_CLOSE, self.kernel)
        # motion_mask = cv2.morphologyEx(motion_mask, cv2.MORPH_OPEN, self.kernel)
        motion_mask = cv2.dilate(motion_mask, self.kernel, iterations=1)

        self.motion_mask = Mask(motion_mask)
        return self.motion_mask.apply(image_np)


class TrackingAnnotation(object):

    def __init__(self, image_np, annotation):
        """ `Annotation` wrapper that applies meanshift to keep an annotation centered on its target.

        :param image_np: initial image
        :param annotation: `Annotation` instance
        """
        self.uuid = str(uuid.uuid4())
        self.annotation = annotation
        self.height, self.width = image_np.shape[:2]
        self.init_tracker(image_np)

    def init_tracker(self, image_np):
        y1, x1, y2, x2 = self.annotation.rect.translate(self.height, self.width)
        self.track_window = (x1, y1, x2 - x1, y2 - y1)
        self.tracker = cv2.TrackerCSRT_create()
        self.tracker.init(image_np, self.track_window)

    def step(self, image_np):
        """ Updates the wrapped annotation based on the data present in `image_np`

        :param image_np: the current image frame
        """

        ret, self.track_window = self.tracker.update(image_np)
        x, y, width, height = self.track_window

        self.annotation.rect.x1 = x / self.width
        self.annotation.rect.x2 = (x + width) / self.width

        self.annotation.rect.y1 = y / self.height
        self.annotation.rect.y2 = (y + height) / self.height

        # cv2.imshow("{} - {}".format(self.label['name'], self.uuid), image_np)

    # def __del__(self):
    #     cv2.destroyWindow("{} - {}".format(self.label['name'], self.uuid))

    def __getattr__(self, item):
        """ Present so the underlying annotation object's content is accessible.
        """
        return getattr(self.annotation, item)


class AnnotationHandler(object):
    def apply_first(self, annotation, image_np):
        return self.apply(annotation, image_np)

    def apply(self, annotation, image_np):
        pass


class AnnotationTracker(AnnotationHandler):
    def apply(self, annotation, image_np):
        pass


class AnnotationCropper(AnnotationHandler):
    def apply(self):
        frame_time = time.time()
        height, width = self.anno_frame.shape[:2]
        for i, annotation in enumerate(self.annotations):
            y1, x1, y2, x2 = annotation.rect.translate(height, width)
            anno_height = y2 - y1
            anno_width = x2 - x1
            if annotation.score >= self.min_confidence and anno_height * anno_width >= 400:
                label = "{}_{}.jpg".format(annotation.label['name'], int(frame_time))
                path = os.path.join(self.crop_dir, label)
                cv2.imwrite(path, annotation.crop(self.anno_frame))


class ImageAnnotator(ImageHandler):

    def __init__(self, graph, label_file, num_class, crop_dir=None, min_confidence=0.5):
        """ Annotates images using an `AnnotationProcessor` subprocess

        :param graph_file: (string) Path to the object detection model's frozen_inference_graph.pb
        :param label_file: (string) Path to the object detection model's labels.pbtxt
        :param num_classes: (int) Number of classes the model is trained on (this is 90 for a coco trained model)
        :param crop_dir: (string) Path to directory where annotation crops should be stored
        :param min_confidence: (float) Minimum confidence score for annotations
        """
        self.crop_dir = crop_dir
        self.min_confidence = min_confidence
        self.frame_queue = Queue()
        self.annotation_queue = Queue()
        self.processor = AnnotationProcessor(graph, label_file, num_class, self.frame_queue, self.annotation_queue)
        self.processor.start()
        self.annotations = []
        self.colors = []
        self.lag_frames = []

    def apply_first(self, image_np):
        self.anno_frame = image_np.copy()
        self.frame_queue.put(image_np)
        self.last_annotation = time.time()
        return image_np

    def apply(self, image_np):
        try:
            annotations = [anno for anno in self.annotation_queue.get_nowait() if anno.label['name'] == 'person']
        except queue.Empty:
            for annotation in self.annotations:
                annotation.step(image_np)
        else:
            self.colors = []
            final_annos = []
            for anno in annotations:
                for existing_anno in self.annotations:
                    if anno.label != existing_anno.label:
                        continue

                    if 0.85 <= existing_anno.rect.area() / anno.rect.area() <= 1.15:
                        overlap = anno.rect.overlap(existing_anno.rect)
                        if overlap and overlap > 0.7:
                            self.colors.append((0, 255, 0))
                            existing_anno.rect = anno.rect
                            existing_anno.init_tracker(self.anno_frame)
                            final_annos.append(existing_anno)
                            break
                else:
                    self.colors.append((0, 0, 255))
                    final_annos.append(TrackingAnnotation(self.anno_frame, anno))
            self.annotations = final_annos
            self.anno_frame = image_np.copy()
            self.frame_queue.put(image_np)


        self.draw_annotations(image_np)

        return image_np

    def close(self):
        self.frame_queue.put(None)

    def draw_annotations(self, image_np):
        for color, annotation in zip(self.colors, self.annotations):
            if annotation.score >= self.min_confidence:
                annotation.draw(image_np, color)

    def crop_annotations(self):
        frame_time = time.time()
        height, width = self.anno_frame.shape[:2]
        for i, annotation in enumerate(self.annotations):
            y1, x1, y2, x2 = annotation.rect.translate(height, width)
            anno_height = y2 - y1
            anno_width = x2 - x1
            if annotation.score >= self.min_confidence and anno_height * anno_width >= 400:
                label = "{}_{}.jpg".format(annotation.label['name'], int(frame_time))
                path = os.path.join(self.crop_dir, label)
                cv2.imwrite(path, annotation.crop(self.anno_frame))


class AVIOutput(ImageHandler):

    def __init__(self, output_path):
        self.output_path = output_path
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        self._output = cv2.VideoWriter(output_path, fourcc, 20.0, (640, 480))

    def apply_first(self, image_np):
        return self.apply(image_np)

    def apply(self, image_np):
        self._output.write(image_np)
        return image_np

    def close(self):
        self._output.release()


class FPSCounter(ImageHandler):

    def __init__(self):
        self.last_time = time.time()

    def apply_first(self, image_np):
        return self.apply(image_np)

    def apply(self, image_np):
        font = cv2.FONT_HERSHEY_PLAIN
        fontScale = 1
        lineType = 2
        cv2.putText(image_np, str(int(1 / (time.time() - self.last_time))) + " FPS", (10, 40), font,
                    fontScale,
                    (0, 0, 255),
                    lineType)
        self.last_time = time.time()
        return image_np


class Command(BaseCommand):
    help = ''

    def __init__(self):
        super(Command, self).__init__()
        self._last_time = 0

    def add_arguments(self, parser):

        parser.add_argument('graph')
        parser.add_argument('label_file')
        parser.add_argument('num_classes', type=int)
        parser.add_argument('--min_confidence', type=float, default=0.5)
        parser.add_argument('--show_fps', action="store_true", default=False)
        parser.add_argument('--avi_out', default=None)
        parser.add_argument('--crop_dir', default=None)
        parser.add_argument('--subtract_bg', action="store_true", default=False)

    def setup_pipeline(self, options):
        pipeline = []

        if options['subtract_bg']:
            pipeline.append(BackgroundSubtractor())

        pipeline.append(
            ImageAnnotator(options['graph'], options['label_file'], options['num_classes'],
                           options['crop_dir'], options['min_confidence'])
        )

        if options['show_fps']:
            pipeline.append(FPSCounter())

        if options['avi_out']:
            pipeline.append(AVIOutput(options['avi_out']))

        return pipeline

    def handle(self, *args, **options):

        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        pipeline = self.setup_pipeline(options)

        ret, frame = cap.read()
        for handler in pipeline:
            frame = handler.apply_first(frame)

        while (True):
            ret, frame = cap.read()

            for handler in pipeline:
                frame = handler.apply(frame)

            cv2.imshow('frame', frame)
            try:
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    for handler in pipeline:
                        handler.close()
                    break
            except KeyboardInterrupt:
                for handler in pipeline:
                    handler.close()
                raise

        cap.release()
        cv2.destroyAllWindows()
