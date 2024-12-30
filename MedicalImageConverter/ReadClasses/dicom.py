
import os
import copy
import time
import gdcm
import threading

import numpy as np
import pandas as pd
import pydicom as dicom
from pydicom.uid import generate_uid

from ..DataClasses import Image


def thread_process_dicom(path, stop_before_pixels=False):
    try:
        datasets = dicom.dcmread(str(path), stop_before_pixels=stop_before_pixels)
    except:
        datasets = []

    return datasets


class DicomReader:
    def __init__(self, reader):
        self.reader = reader

        self.ds = []
        self.ds_modality = {key: [] for key in ['CT', 'MR', 'PT', 'US', 'DX', 'MG', 'NM', 'XA', 'CR', 'RTSTRUCT', 'REG',
                                          'RTDose']}

    def add_dicom_extension(self):
        for ii, name in enumerate(self.reader.files['Dicom']):
            a, b = os.path.splitext(name)
            if not b:
                self.reader.files['Dicom'][ii] = name + '.dcm'

    def load_dicoms(self, display_time=True):
        t1 = time.time()
        self.read()
        self.separate_modalities_and_images()
        self.image_creation()

        # if not only_tags:
        #     self.convert_images()
        #     self.fix_orientation()
        #     self.separate_contours()
        t2 = time.time()

        if display_time:
            print('Dicom Read Time: ', t2 - t1)

    def read(self):
        """
        Reads in the dicom files using a threading process, and the user input "only_tags" determines if only the tags
        are loaded or the tags and array.

        """
        threads = []

        def read_file_thread(file_path):
            self.ds.append(thread_process_dicom(file_path, stop_before_pixels=self.reader.only_tags))

        for file_path in self.reader.files['Dicom']:
            thread = threading.Thread(target=read_file_thread, args=(file_path,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

    def separate_modalities_and_images(self):
        """
        Separate read in files by their modality and image based on SeriesInstanceUID and AcquisitionNumber.
        US and DX (X-ray) are considered 2d images, therefore they don't require image separation, because each file
        is considered to be a unique image, even though US can have multiple "slices" per file each slice will be
        considered a 2d image.

        ds_modality - dictionary of different modalities
        Returns
        -------

        """
        for modality in list(self.ds_modality.keys()):
            images_in_modality = [d for d in self.ds if d['Modality'].value == modality]
            if len(images_in_modality) > 0 and modality in self.reader.only_modality:
                if modality not in ['US', 'DX', 'MG', 'XA', 'CR', 'RTSTRUCT', 'REG', 'RTDose']:
                    sorting_tags = np.asarray([[img['SeriesInstanceUID'].value, img['AcquisitionNumber'].value] if
                                               'AcquisitionNumber' in img and img['AcquisitionNumber'].value is not None
                                               else [img['SeriesInstanceUID'].value, 1] for img in images_in_modality])

                    unique_tags = np.unique(sorting_tags, axis=0)
                    for tag in unique_tags:
                        sorted_idx = np.where((sorting_tags[:, 0] == tag[0]) & (sorting_tags[:, 1] == tag[1]))
                        image_tags = [images_in_modality[idx] for idx in sorted_idx[0]]

                        if 'ImageOrientationPatient' in image_tags[0] and 'ImagePositionPatient' in image_tags[0]:
                            orientations = np.asarray([img['ImageOrientationPatient'].value for img in image_tags])
                            unique_orientations = np.unique(orientations, axis=0)
                            for orientation in unique_orientations:
                                orient_idx = np.where((orientations[:, 0] == orientation[0]) &
                                                      (orientations[:, 1] == orientation[1]) &
                                                      (orientations[:, 2] == orientation[2]) &
                                                      (orientations[:, 3] == orientation[3]) &
                                                      (orientations[:, 4] == orientation[4]) &
                                                      (orientations[:, 5] == orientation[5]))

                                orient_tags = [image_tags[idx] for idx in orient_idx[0]]
                                orientation = orient_tags[0]['ImageOrientationPatient'].value
                                position_tags = np.asarray([t['ImagePositionPatient'].value for t in orient_tags])

                                x = np.abs(orientation[0]) + np.abs(orientation[3])
                                y = np.abs(orientation[1]) + np.abs(orientation[4])
                                z = np.abs(orientation[2]) + np.abs(orientation[5])

                                if x < y and x < z:
                                    slice_idx = np.argsort(position_tags[:, 0])
                                elif y < x and y < z:
                                    slice_idx = np.argsort(position_tags[:, 1])
                                else:
                                    slice_idx = np.argsort(position_tags[:, 2])

                                self.ds_modality[modality] += [[orient_tags[idx] for idx in slice_idx]]

                elif modality in ['US', 'DX', 'MG', 'XA', 'CR', 'RTSTRUCT', 'REG', 'RTDose']:
                    for image in images_in_modality:
                        self.ds_modality[modality] += [image]

    def image_creation(self):
        images = []
        for modality in list(self.ds_modality.keys()):
            for image_set in self.ds_modality[modality]:
                if modality in ['CT', 'MR']:
                    images += [Image3d(image_set, self.reader.only_tags)]

                # elif modality in ['PT', 'NM']:
                #     reference_tags = [image.image_set[0] for image in images]
                #     images += [ImageNucMed(image_set, self.only_tags, reference_tags)]

                elif modality == 'DX':
                    images += [ImageDX(image_set, self.reader.only_tags)]

                elif modality == 'MG':
                    if 'ImageType' in image_set:
                        if 'VOLUME' in image_set['ImageType'].value or 'TOMOSYNTHESIS' in image_set['ImageType'].value:
                            pass
                            # images += [ImageMG(image_set, self.reader.only_tags)]

                        else:
                            images += [ImageDX(image_set, self.reader.only_tags)]

                elif modality == 'RTSTRUCT':
                    rtsruct = RTStruct(image_set)

        print(1)


class Image3d(object):
    """
    Important tags are extracted for dicom files along with array if only_tags=False.
        Tags:
            plane - main view plane of original image
            spacing - the inplane spacing and the slice thickness are combined
                    - Note: the slice thickness is recalculated during compute_image_matrix because sometimes the tag
                      is incorrectly saved
            orientation - the direction cosine matrix
            origin - the origin and array are recomputed to be "Feet-first supine" (FFS) if it is another position
            image_matrix - this is a 4x4 matrix illustrates the rotation of the image,
                         - Note: The is a function that will look for skipped slices which sometimes occur between
                           abdomen and pelvis, this will interpolate a slice that is missing
    """
    def __init__(self, image_set, only_tags):
        self.image_set = image_set
        self.only_tags = only_tags

        self.unverified = None
        self.base_position = None
        self.skipped_slice = None
        self.sections = None
        self.rgb = False

        if not self.only_tags:
            self.array = self._compute_array()

        self.filepaths = [image.filename for image in self.image_set]
        self.sops = [image.SOPInstanceUID for image in self.image_set]
        self.plane = self._compute_plane()
        self.spacing = self._compute_spacing()
        self.dimensions = self._compute_dimensions()
        self.orientation = self._compute_orientation()
        self.origin = self._compute_origin()
        self.image_matrix = self._compute_image_matrix()

    def _compute_array(self):
        image_slices = []
        for _slice in self.image_set:
            if (0x0028, 0x1052) in _slice:
                intercept = _slice.RescaleIntercept
            else:
                intercept = 0

            if (0x0028, 0x1053) in _slice:
                slope = _slice.RescaleSlope
            else:
                slope = 1

            image_slices.append(((_slice.pixel_array*slope)+intercept).astype('int16'))

            del _slice.PixelData

        image_hold = np.asarray(image_slices)
        if len(image_hold.shape) > 3:
            return image_hold[0]
        else:
            return image_hold

    def _compute_plane(self):
        orientation = self.image_set[0]['ImageOrientationPatient'].value
        x = np.abs(orientation[0]) + np.abs(orientation[3])
        y = np.abs(orientation[1]) + np.abs(orientation[4])
        z = np.abs(orientation[2]) + np.abs(orientation[5])

        if x < y and x < z:
            return 'Sagittal'
        elif y < x and y < z:
            return 'Coronal'
        else:
            return 'Axial'

    def _compute_spacing(self):
        inplane_spacing = [1, 1]
        slice_thickness = np.double(self.image_set[0].SliceThickness)

        if 'PixelSpacing' in self.image_set[0]:
            inplane_spacing = self.image_set[0].PixelSpacing

        elif 'ContributingSourcesSequence' in self.image_set[0]:
            sequence = 'ContributingSourcesSequence'
            if 'DetectorElementSpacing' in self.image_set[0][sequence][0]:
                inplane_spacing = self.image_set[0][sequence][0]['DetectorElementSpacing']

        elif 'PerFrameFunctionalGroupsSequence' in self.image_set[0]:
            sequence = 'PerFrameFunctionalGroupsSequence'
            if 'PixelMeasuresSequence' in self.image_set[0][sequence][0]:
                inplane_spacing = self.image_set[0][sequence][0]['PixelMeasuresSequence'][0]['PixelSpacing']

        return np.asarray([inplane_spacing[0], inplane_spacing[1], slice_thickness])

    def _compute_dimensions(self):
        return np.asarray([self.image_set[0]['Columns'].value, self.image_set[0]['Rows'].value, len(self.image_set)])

    def _compute_orientation(self):
        orientation = np.asarray([1, 0, 0, 0, 1, 0])
        if 'ImageOrientationPatient' in self.image_set[0]:
            orientation = np.asarray(self.image_set[0]['ImageOrientationPatient'].value)

        else:
            if 'SharedFunctionalGroupsSequence' in self.image_set[0]:
                seq_str = 'SharedFunctionalGroupsSequence'
                if 'PlaneOrientationSequence' in self.image_set[0][0][seq_str][0]:
                    plane_str = 'PlaneOrientationSequence'
                    image_str = 'ImageOrientationPatient'
                    orientation = np.asarray(self.image_set[0][0][seq_str][0][plane_str][0][image_str].value)

                else:
                    self.unverified = 'Orientation'

            else:
                self.unverified = 'Orientation'

        return orientation

    def _compute_origin(self):
        origin = np.asarray(self.image_set[0]['ImagePositionPatient'].value)
        if 'PatientPosition' in self.image_set[0]:
            self.base_position = self.image_set[0]['PatientPosition'].value

            if self.base_position in ['HFDR', 'FFDR']:
                if self.only_tags:
                    self.array = np.rot90(self.array, 3, (1, 2))

                origin[0] = np.double(origin[0]) - self.spacing[0] * (self.dimensions[0] - 1)
                self.orientation = [-self.orientation[3], -self.orientation[4], -self.orientation[5],
                                    self.orientation[0], self.orientation[1], self.orientation[2]]

            elif self.base_position in ['HFP', 'FFP']:
                if self.only_tags:
                    self.array = np.rot90(self.array, 2, (1, 2))

                origin[0] = np.double(origin[0]) - self.spacing[0] * (self.dimensions[0] - 1)
                origin[1] = np.double(origin[1]) - self.spacing[1] * (self.dimensions[1] - 1)
                self.orientation = -np.asarray(self.orientation)

            elif self.base_position in ['HFDL', 'FFDL']:
                if self.only_tags:
                    self.array = np.rot90(self.array, 1, (1, 2))

                origin[1] = np.double(origin[1]) - self.spacing[1] * (self.dimensions[1] - 1)
                self.orientation = [self.orientation[3], self.orientation[4], self.orientation[5],
                                    -self.orientation[0], -self.orientation[1], -self.orientation[2]]

        return origin

    def _compute_image_matrix(self):
        row_direction = self.orientation[:3]
        column_direction = self.orientation[3:]

        slice_direction = np.cross(row_direction, column_direction)
        if len(self.image_set) > 1:
            first = np.dot(slice_direction, self.image_set[0].ImagePositionPatient)
            second = np.dot(slice_direction, self.image_set[1].ImagePositionPatient)
            last = np.dot(slice_direction, self.image_set[-1].ImagePositionPatient)
            first_last_spacing = np.asarray((last - first) / (len(self.image_set) - 1))
            if np.abs((second - first) - first_last_spacing) > 0.01:
                if not self.only_tags:
                    self._find_skipped_slices(slice_direction)
                slice_spacing = second - first
            else:
                slice_spacing = np.asarray((last - first) / (len(self.image_set) - 1))

            self.spacing[2] = slice_spacing

        mat = np.identity(4, dtype=np.float32)
        mat[0, :3] = row_direction
        mat[1, :3] = column_direction
        mat[2, :3] = slice_direction
        mat[0:3, 3] = -self.origin

        return mat

    def _find_skipped_slices(self, slice_direction):
        base_spacing = None
        for ii in range(len(self.image_set) - 1):
            position_1 = np.dot(slice_direction, self.image_set[ii].ImagePositionPatient)
            position_2 = np.dot(slice_direction, self.image_set[ii + 1].ImagePositionPatient)
            if ii == 0:
                base_spacing = position_2 - position_1
            if ii > 0 and np.abs(base_spacing - (position_2 - position_1)) > 0.01:
                print(ii)
                self.unverified = 'Skipped'
                self.skipped_slice = ii + 1
                self.dimensions[2] += 1
                self.filepaths.insert(ii + 1, '')
                self.sops.insert(ii + 1, '1.123456789')

                hold_data = copy.deepcopy(self.array)
                interpolate_slice = np.mean(self.array[ii:ii + 2, :, :], axis=0).astype(np.int16)
                self.array = np.insert(hold_data, self.skipped_slice, interpolate_slice, axis=0)

    def axial_correction(self):
        if self.plane == 'Sagittal':
            array_hold = copy.deepcopy(self.array)
            array_hold = np.swapaxes(array_hold, 0, 1)
            array_hold = np.swapaxes(array_hold, 1, 2)
            self.array = np.flip(array_hold, axis=0)

            self.orientation[0:2] = 1 - self.orientation[0:2]
            self.orientation[4] = 1 - self.orientation[4]
            self.orientation[5] = 1 + self.orientation[5]

        elif self.plane == 'Coronal':
            array_hold = copy.deepcopy(self.array)
            self.array = np.flip(array_hold, axis=0)

            self.orientation[4] = 1 - self.orientation[4]
            self.orientation[5] = 1 + self.orientation[5]


class ImageDX(object):
    def __init__(self, image_set, only_tags):
        self.image_set = image_set
        self.only_tags = only_tags

        self.unverified = 'Modality'
        self.base_position = self.image_set.PatientOrientation
        self.skipped_slice = None
        self.sections = None
        self.rgb = False

        self.filepaths = self.image_set.filename
        self.sops = self.image_set.SOPInstanceUID
        self.plane = self.image_set.ViewPosition
        self.orientation = [1, 0, 0, 0, 1, 0]
        self.origin = np.asarray([0, 0, 0])
        self.image_matrix = np.identity(4, dtype=np.float32)
        self.dimensions = np.asarray([self.image_set['Columns'].value, self.image_set['Rows'].value, 1])

        if not self.only_tags:
            self.array = self._compute_array()
        self.spacing = self._compute_spacing()

    def _compute_array(self):
        array = self.image_set.pixel_array.astype('int16')
        del self.image_set.PixelData

        if 'PresentationLUTShape' in self.image_set and self.image_set['PresentationLUTShape'] == 'Inverse':
            array = 16383 - array

        return array.reshape((1, array.shape[0], array.shape[1]))

    def _compute_spacing(self):
        inplane_spacing = [1, 1]
        slice_thickness = 1

        if 'PixelSpacing' in self.image_set:
            inplane_spacing = self.image_set.PixelSpacing

        elif 'ContributingSourcesSequence' in self.image_set:
            sequence = 'ContributingSourcesSequence'
            if 'DetectorElementSpacing' in self.image_set[sequence][0]:
                inplane_spacing = self.image_set[sequence][0]['DetectorElementSpacing']

        elif 'PerFrameFunctionalGroupsSequence' in self.image_set:
            sequence = 'PerFrameFunctionalGroupsSequence'
            if 'PixelMeasuresSequence' in self.image_set[sequence][0]:
                inplane_spacing = self.image_set[sequence][0]['PixelMeasuresSequence'][0]['PixelSpacing']

        return np.asarray([inplane_spacing[0], inplane_spacing[1], slice_thickness])


class ImageMG(object):
    def __init__(self, image_set, only_tags):
        self.image_set = image_set
        self.only_tags = only_tags

        self.unverified = 'Modality'
        self.base_position = self.image_set.PatientOrientation
        self.skipped_slice = None
        self.sections = None
        self.rgb = False

        self.filepaths = self.image_set.filename
        self.sops = self.image_set.SOPInstanceUID
        self.origin = np.asarray([0, 0, 0])

        if not self.only_tags:
            self.array = self._compute_array()
        self.spacing = self._compute_spacing()
        self.dimensions = self._compute_dimensions()
        self.orientation = self._compute_orientation()
        self.plane = self._compute_plane
        # self.image_matrix = self._compute_image_matrix()

    def _compute_array(self):
        if (0x0028, 0x1052) in self.image_set:
            intercept = self.image_set.RescaleIntercept
        else:
            intercept = 0

        if (0x0028, 0x1053) in self.image_set:
            slope = self.image_set.RescaleSlope
        else:
            slope = 1

        array = ((self.image_set.pixel_array*slope)+intercept).astype('int16')

        del self.image_set.PixelData

        return array

    def _compute_plane(self):
        x = np.abs(self.orientation[0]) + np.abs(self.orientation[3])
        y = np.abs(self.orientation[1]) + np.abs(self.orientation[4])
        z = np.abs(self.orientation[2]) + np.abs(self.orientation[5])

        if x < y and x < z:
            return 'Sagittal'
        elif y < x and y < z:
            return 'Coronal'
        else:
            return 'Axial'

    def _compute_spacing(self):
        inplane_spacing = [1, 1]
        slice_thickness = 1

        if 'PixelSpacing' in self.image_set:
            inplane_spacing = self.image_set.PixelSpacing

        elif 'ContributingSourcesSequence' in self.image_set:
            sequence = 'ContributingSourcesSequence'
            if 'DetectorElementSpacing' in self.image_set[sequence][0]:
                inplane_spacing = self.image_set[sequence][0]['DetectorElementSpacing']

        elif 'PerFrameFunctionalGroupsSequence' in self.image_set:
            sequence = 'PerFrameFunctionalGroupsSequence'
            if 'PixelMeasuresSequence' in self.image_set[sequence][0]:
                inplane_spacing = self.image_set[sequence][0]['PixelMeasuresSequence'][0]['PixelSpacing']

        return np.asarray([inplane_spacing[0], inplane_spacing[1], slice_thickness])

    def _compute_dimensions(self):
        if self.array is not None:
            slices = self.array.shape[0]
        else:
            slices = 1
        return np.asarray([self.image_set['Columns'].value, self.image_set['Rows'].value, slices])

    def _compute_orientation(self):
        orientation = np.asarray([1, 0, 0, 0, 1, 0])
        if 'ImageOrientationPatient' in self.image_set:
            orientation = np.asarray(self.image_set['ImageOrientationPatient'].value)

        else:
            if 'SharedFunctionalGroupsSequence' in self.image_set:
                seq_str = 'SharedFunctionalGroupsSequence'
                if 'PlaneOrientationSequence' in self.image_set[seq_str][0]:
                    plane_str = 'PlaneOrientationSequence'
                    image_str = 'ImageOrientationPatient'
                    orientation = np.asarray(self.image_set[seq_str][0][plane_str][0][image_str].value)

                else:
                    self.unverified = 'Orientation'

            else:
                self.unverified = 'Orientation'

        return orientation

    def _compute_image_matrix(self):
        row_direction = self.orientation[:3]
        column_direction = self.orientation[3:]

        slice_direction = np.cross(row_direction, column_direction)
        if len(self.image_set) > 1:
            first = np.dot(slice_direction, self.image_set[0].ImagePositionPatient)
            last = np.dot(slice_direction, self.image_set[-1].ImagePositionPatient)

            self.spacing[2] = np.asarray((last - first) / (len(self.image_set) - 1))

        mat = np.identity(4, dtype=np.float32)
        mat[0, :3] = row_direction
        mat[1, :3] = column_direction
        mat[2, :3] = slice_direction
        mat[0:3, 3] = -self.origin

        return mat


class RTStruct(object):
    def __init__(self, image_set, reference_tags):
        self.image_set = image_set

