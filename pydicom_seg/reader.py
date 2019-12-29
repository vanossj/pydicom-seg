import abc
import enum

import attr
import numpy as np
import pydicom
import SimpleITK as sitk


class AlgorithmType(enum.Enum):
    AUTOMATIC = 'AUTOMATIC'
    SEMIAUTOMATIC = 'SEMIAUTOMATIC'
    MANUAL = 'MANUAL'


class SegmentsOverlap(enum.Enum):
    YES = 'YES'
    UNDEFINED = 'UNDEFINED'
    NO = 'NO'


class SegmentationType(enum.Enum):
    BINARY = 'BINARY'
    FRACTIONAL = 'FRACTIONAL'


@attr.s
class Code:
    value = attr.ib()
    coding_scheme_designator = attr.ib()
    meaning = attr.ib()


@attr.s
class SegmentInfo:
    number = attr.ib(type=int)
    label = attr.ib(type=str)
    description = attr.ib(type=str)
    algorithm_type = attr.ib(type=AlgorithmType)
    property_category = attr.ib(factory=Code)
    property_type = attr.ib(factory=Code)


class _ReaderBase(abc.ABC):
    def __init__(self, dataset: pydicom.Dataset):
        self.dataset = dataset

        self.segment_infos = {}
        self._decode_segments(dataset)

        self.spacing = self._get_declared_image_spacing()
        self.direction = self._get_image_direction()
        self.origin, extent = self._get_image_origin_and_extent(self.direction)
        self.size = (dataset.Rows, dataset.Columns, int(np.ceil(extent / self.spacing[-1]) + 1))

        self._decode()

    @property
    def referenced_series_uid(self):
        return self.dataset.ReferencedSeriesSequence[0].SeriesInstanceUID

    @property
    def referenced_instance_uids(self):
        return [
            x.ReferencedSOPInstanceUID
            for x in self.dataset.ReferencedSeriesSequence[0].ReferencedInstanceSequence
        ]

    @abc.abstractmethod
    def _decode(self):
        pass

    def _decode_segments(self, dataset: pydicom.Dataset):
        for segment in dataset.SegmentSequence:
            if segment.SegmentNumber in self.segment_infos:
                raise ValueError(f'Segment {segment.SegmentNumber} was declared more than once.')

            self.segment_infos[segment.SegmentNumber] = SegmentInfo(
                property_category=Code(
                    value=segment.SegmentedPropertyCategoryCodeSequence[0].CodeValue,
                    coding_scheme_designator=segment.SegmentedPropertyCategoryCodeSequence[0].CodingSchemeDesignator,
                    meaning=segment.SegmentedPropertyCategoryCodeSequence[0].CodeMeaning
                ),
                number=segment.SegmentNumber,
                label=segment.SegmentLabel,
                description=segment.get('SegmentDescription', ''),
                algorithm_type=AlgorithmType[segment.SegmentAlgorithmType],
                property_type=Code(
                    value=segment.SegmentedPropertyTypeCodeSequence[0].CodeValue,
                    coding_scheme_designator=segment.SegmentedPropertyTypeCodeSequence[0].CodingSchemeDesignator,
                    meaning=segment.SegmentedPropertyTypeCodeSequence[0].CodeMeaning
                )
            )

    def _get_declared_image_spacing(self):
        sfg = self.dataset.SharedFunctionalGroupsSequence[0]
        if 'PixelMeasuresSequence' not in sfg:
            raise ValueError('Pixel measures FG is missing!')

        pixel_measures = sfg.PixelMeasuresSequence[0]
        x_spacing, y_spacing = pixel_measures.PixelSpacing
        if 'SpacingBetweenSlices' in pixel_measures:
            z_spacing = pixel_measures.SpacingBetweenSlices
        else:
            z_spacing = pixel_measures.SliceThickness

        return float(x_spacing), float(y_spacing), float(z_spacing)

    def _get_image_direction(self):
        sfg = self.dataset.SharedFunctionalGroupsSequence[0]
        if 'PlaneOrientationSequence' not in sfg:
            raise ValueError('Plane Orientation (Patient) is missing')

        iop = sfg.PlaneOrientationSequence[0].ImageOrientationPatient
        assert len(iop) == 6

        # Extract x-vector and y-vector
        x_dir = [float(x) for x in iop[:3]]
        y_dir = [float(x) for x in iop[3:]]

        # L2 normalize x-vector and y-vector
        x_dir /= np.linalg.norm(x_dir)
        y_dir /= np.linalg.norm(y_dir)

        # Compute perpendicular z-vector
        z_dir = np.cross(x_dir, y_dir)

        # TODO Maybe incorrect, transpose needed?
        return np.stack([x_dir, y_dir, z_dir], axis=1)

    def _get_image_origin_and_extent(self, direction: np.ndarray):
        frames = self.dataset.PerFrameFunctionalGroupsSequence
        slice_dir = direction[:, 2]
        reference_position = np.asarray([float(x) for x in frames[0].PlanePositionSequence[0].ImagePositionPatient])

        min_distance = None
        origin = None
        distances = {}
        for frame_idx, frame in enumerate(frames):
            frame_position = tuple(float(x) for x in frame.PlanePositionSequence[0].ImagePositionPatient)
            if frame_position in distances:
                continue

            frame_distance = np.dot(frame_position - reference_position, slice_dir)
            distances[frame_position] = frame_distance

            if frame_idx == 0 or frame_distance < min_distance:
                min_distance = frame_distance
                origin = frame_position

        # Sort all distances ascending and compute extent from minimum and
        # maximum distance to reference plane
        distances = sorted(distances.values())
        extent = 0.0
        if len(distances) > 1:
            extent = abs(distances[0] - distances[-1])

        return origin, extent


class SegmentReader(_ReaderBase):
    def _decode(self):
        # SimpleITK has currently no support for writing slices into memory, allocate a numpy array
        # as intermediate buffer and create an image afterwards
        segmentation_type = SegmentationType[self.dataset.SegmentationType]
        dtype = np.uint8 if segmentation_type == SegmentationType.BINARY else np.float32
        segment_buffer = np.zeros(self.size[::-1], dtype=dtype)

        self.segment_images = {}
        for segment_number in self.segment_infos:
            # Dummy image for computing indices from physical points
            dummy = sitk.Image(1, 1, 1, sitk.sitkUInt8)
            dummy.SetOrigin(self.origin)
            dummy.SetSpacing(self.spacing)
            dummy.SetDirection(self.direction.ravel())

            # Iterate over all frames and check for referenced segment number
            for frame_idx, pffg in enumerate(self.dataset.PerFrameFunctionalGroupsSequence):
                if segment_number != pffg.SegmentIdentificationSequence[0].ReferencedSegmentNumber:
                    continue
                frame_position = [float(x) for x in pffg.PlanePositionSequence[0].ImagePositionPatient]
                frame_index = dummy.TransformPhysicalPointToIndex(frame_position)
                slice_data = self.dataset.pixel_array[frame_idx]

                # If it is fractional data, then convert to range [0, 1]
                if segmentation_type == SegmentationType.FRACTIONAL:
                    slice_data = slice_data.astype(dtype) / self.dataset.MaximumFractionalValue

                segment_buffer[frame_index[2]] = slice_data

            # Construct final SimpleITK image from numpy array
            image = sitk.GetImageFromArray(segment_buffer)
            image.SetOrigin(self.origin)
            image.SetSpacing(self.spacing)
            image.SetDirection(self.direction.ravel())

            self.segment_images[segment_number] = image


class MultiClassReader(_ReaderBase):
    def _decode(self):
        # Multi-class decoding assumes binary segmentations
        segmentation_type = SegmentationType(self.dataset.SegmentationType)
        if segmentation_type != SegmentationType.BINARY:
            raise ValueError('Invalid segmentation type, only BINARY is supported for decoding multi-class segmentations.')

        # Multi-class decoding requires non-overlapping segmentations
        segments_overlap = SegmentsOverlap(self.dataset.get('SegmentsOverlap', SegmentsOverlap.UNDEFINED))
        if segments_overlap == SegmentsOverlap.YES:
            raise ValueError('Segmentation contains overlapping segments, cannot read as multi-class.')

        # Choose suitable data format for multi-class segmentions, depending
        # on the number of segments
        max_segment_number = max(self.segment_infos.keys())
        if max_segment_number < 256:
            dtype = np.uint8
        else:
            dtype = np.uint16

        # SimpleITK has currently no support for writing slices into memory, allocate a numpy array
        # as intermediate buffer and create an image afterwards
        segment_buffer = np.zeros(self.size[::-1], dtype=dtype)

        # Dummy image for computing indices from physical points
        dummy = sitk.Image(1, 1, 1, sitk.sitkUInt8)
        dummy.SetOrigin(self.origin)
        dummy.SetSpacing(self.spacing)
        dummy.SetDirection(self.direction.ravel())

        # Iterate over all frames and update buffer with segment mask
        for frame_id, pffg in enumerate(self.dataset.PerFrameFunctionalGroupsSequence):
            referenced_segment_number = pffg.SegmentIdentificationSequence[0].ReferencedSegmentNumber
            frame_position = [float(x) for x in pffg.PlanePositionSequence[0].ImagePositionPatient]
            frame_index = dummy.TransformPhysicalPointToIndex(frame_position)
            segment_buffer[frame_index[2]][np.greater(self.dataset.pixel_array[frame_id], 0)] = referenced_segment_number

        # Construct final SimpleITK image from numpy array
        self.image = sitk.GetImageFromArray(segment_buffer)
        self.image.SetOrigin(self.origin)
        self.image.SetSpacing(self.spacing)
        self.image.SetDirection(self.direction.ravel())