from logging import getLogger

from cam_server.pipeline.data_processing import functions
from cam_server.utils import create_thread_pvs, epics_lock


import json

import numpy
import scipy.signal
import scipy.optimize
import numba

numba.set_num_threads(4)

_logger = getLogger(__name__)

channel_names = None
output_pv, center_pv, fwhm_pv, ymin_pv, ymax_pv, axis_pv = None, None, None, None, None, None
roi = [0, 0]
initialized = False
sent_pid = -1


@numba.njit(parallel=True)
def get_spectrum(image, background):
    y = image.shape[0]
    x = image.shape[1]

    profile = numpy.zeros(x, dtype=numpy.uint32)

    for i in numba.prange(y):
        for j in range(x):
            v = image[i, j]
            b = background[i, j]
            if v > b:
                v -= b*
            else:
                #v -= b # added this change to remove +ve noise contribution
                v = 0
            profile[j] += v
    return profile


def initialize(parameters):
    global ymin_pv, ymax_pv, axis_pv, output_pv, center_pv, fwhm_pv
    global channel_names
    epics_pv_name_prefix = parameters["camera_name"]
    output_pv_name = epics_pv_name_prefix + ":SPECTRUM_Y"
    center_pv_name = epics_pv_name_prefix + ":SPECTRUM_CENTER"
    fwhm_pv_name = epics_pv_name_prefix + ":SPECTRUM_FWHM"
    ymin_pv_name = epics_pv_name_prefix + ":SPC_ROI_YMIN"
    ymax_pv_name = epics_pv_name_prefix + ":SPC_ROI_YMAX"
    axis_pv_name = epics_pv_name_prefix + ":SPECTRUM_X"
    channel_names = [output_pv_name, center_pv_name, fwhm_pv_name, ymin_pv_name, ymax_pv_name, axis_pv_name]


def process_image(image, pulse_id, timestamp, x_axis, y_axis, parameters, bsdata=None, background=None):
    global roi, initialized, sent_pid
    global channel_names
    
    if not initialized:
        initialize(parameters)
        initialized = True
    [output_pv, center_pv, fwhm_pv, ymin_pv, ymax_pv, axis_pv] = create_thread_pvs(channel_names)
    processed_data = dict()
    epics_pv_name_prefix = parameters["camera_name"]

    if ymin_pv and ymin_pv.connected:
        roi[0] = ymin_pv.value
    if ymax_pv and ymax_pv.connected:
        roi[1] = ymax_pv.value
    if axis_pv and axis_pv.connected:
        axis = axis_pv.value
    else:
        axis = None

    if axis is None:
        _logger.warning("Energy axis not connected");
        return None

    if len(axis) < image.shape[1]:
        _logger.warning("Energy axis length %d < image width %d", len(axis), image.shape[1])
        return None

    # match the energy axis to image width
    axis = axis[:image.shape[1]]

    processing_image = image
    nrows, ncols = processing_image.shape

    # validate background data if passive mode (background subtraction handled here)
    background_image = parameters.pop('background_data', None)
    if isinstance(background_image, numpy.ndarray):
        if background_image.shape != processing_image.shape:
            _logger.info("Invalid background shape: %s instead of %s" % (
            str(background_image.shape), str(processing_image.shape)))
            background_image = None
    else:
        background_image = None

    processed_data[epics_pv_name_prefix + ":processing_parameters"] = json.dumps(
        {"roi": roi, "background": None if (background_image is None) else parameters.get('image_background')})

    # crop the image in y direction
    ymin, ymax = int(roi[0]), int(roi[1])
    if nrows >= ymax > ymin >= 0:
        if (nrows != ymax) or (ymin != 0):
            processing_image = processing_image[ymin: ymax, :]
            if background_image is not None:
                background_image = background_image[ymin:ymax, :]

    # remove the background and collapse in y direction to get the spectrum
    if background_image is not None:
        spectrum = get_spectrum(processing_image, background_image)
    else:
        spectrum = processing_image.sum(0, 'uint32')

    # smooth the spectrum with savgol filter with 51 window size and 3rd order polynomial
    smoothed_spectrum = scipy.signal.savgol_filter(spectrum, 51, 3)

    # check wether spectrum has only noise. the average counts per pixel at the peak
    # should be larger than 1.5 to be considered as having real signals.
    minimum, maximum = smoothed_spectrum.min(), smoothed_spectrum.max()
    amplitude = maximum - minimum
    skip = True
    if amplitude > nrows * 1.5:
        skip = False
    # gaussian fitting
    offset, amplitude, center, sigma,perr = gauss_fit_psss2(smoothed_spectrum[::2], axis[::2],
            offset=minimum, amplitude=amplitude, skip=skip, maxfev=20)

    # outputs
    processed_data[epics_pv_name_prefix + ":SPECTRUM_Y"] = spectrum
    processed_data[epics_pv_name_prefix + ":SPECTRUM_X"] = axis
    processed_data[epics_pv_name_prefix + ":SPECTRUM_CENTER"] = numpy.float64(center) 
    processed_data[epics_pv_name_prefix + ":SPECTRUM_FWHM"] = numpy.float64(2.355 * sigma) 
    processed_data[epics_pv_name_prefix + ":FIT_ERR"] = perr

    if epics_lock.acquire(False):
            try:
                if pulse_id > sent_pid:
                    sent_pid = pulse_id
                    if output_pv and output_pv.connected:
                        output_pv.put(processed_data[epics_pv_name_prefix + ":SPECTRUM_Y"])
                        #_logger.debug("caput on %s for pulse_id %s", output_pv, pulse_id)

                    if center_pv and center_pv.connected:
                        center_pv.put(processed_data[epics_pv_name_prefix + ":SPECTRUM_CENTER"])

                    if fwhm_pv and fwhm_pv.connected:
                        fwhm_pv.put(processed_data[epics_pv_name_prefix + ":SPECTRUM_FWHM"])
            finally:
                epics_lock.release()

    return processed_data

##########
### editted functions
##########

def gauss_fit_psss2(profile, axis, **kwargs):
    if axis.shape[0] != profile.shape[0]:
        raise RuntimeError("Invalid axis passed %d %d" % (axis.shape[0], profile.shape[0]))

    offset = kwargs.get('offset', profile.min())  # Minimum is good estimation of offset
    amplitude = kwargs.get('amplitude', profile.max() - offset)  # Max value is a good estimation of amplitude
    center = kwargs.get('center', numpy.dot(axis,
                                            profile) / profile.sum())  # Center of mass is a good estimation of center (mu)
    # Consider gaussian integral is amplitude * sigma * sqrt(2*pi)
    standard_deviation = kwargs.get('standard_deviation', numpy.trapz((profile - offset), x=axis) / (
                amplitude * numpy.sqrt(2 * numpy.pi)))
    maxfev = kwargs.get('maxfev', 5  # the default is 100 * (N + 1), which is over killing

    # If user requests fitting to be skipped, return the estimated parameters.
    if kwargs.get('skip', False):
        return offset, amplitude, center, abs(standard_deviation)

    try:
        optimal_parameter, pcov = scipy.optimize.curve_fit(
            functions._gauss_function, axis, profile.astype("float64"),
            p0=[offset, amplitude, center, standard_deviation],
            jac=functions._gauss_deriv,
            col_deriv=1,
            maxfev=maxfev)
        offset, amplitude, center, standard_deviation = optimal_parameter
        perr = pcov#numpy.sqrt(numpy.diag(pcov))
    except BaseException as e:
        pass

    return offset, amplitude, center, abs(standard_deviation), perr




