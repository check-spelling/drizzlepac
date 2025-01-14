#!/usr/bin/env python

"""Quantify how well MVM products are aligned to GAIA sources found in the image footprint


NOTE: daostarfinder coords are 0-indexed."""

# Standard library imports
import argparse
from datetime import datetime
import os
import random
import string
import sys

# Related third party imports
from astropy.io import fits
from astropy.coordinates import SkyCoord
from astropy.table import Column, Table
import matplotlib.pyplot as plt
import numpy as np

# Local application imports
from drizzlepac.devutils.comparison_tools import compare_sourcelists as csl
from drizzlepac.haputils import astrometric_utils as amutils
from drizzlepac.haputils import cell_utils
from drizzlepac.haputils import comparison_utils as cu
from drizzlepac.haputils import deconvolve_utils as decutils
import stwcs

from stsci.tools import logutil
__taskname__ = 'analyze_mvm_gaia_alignment'

MSG_DATEFMT = '%Y%j%H%M%S'
SPLUNK_MSG_FORMAT = '%(asctime)s %(levelname)s src=%(name)s- %(message)s'
log = logutil.create_logger(__name__, level=logutil.logging.NOTSET, stream=sys.stdout,
                            format=SPLUNK_MSG_FORMAT, datefmt=MSG_DATEFMT)
# ============================================================================================================


def perform(mosaic_imgname, flcflt_list, diagnostic_mode=False, log_level=logutil.logging.INFO, plot_output_dest="none"):
    """ Statistically quantify quality of GAIA MVM alignment

    Parameters
    ----------
    mosaic_imgname : str
        Name of the MVM-processed mosaic image to process

    flcflt_list : list
        lList of calibrated flc.fits and/or flt.fits images to process

    diagnostic_mode : bool, optional
        If set to logical 'True', additional log messages will be displayed and additional files will be
        created during the course of the run. Default value is logical 'False'.

    log_level : int, optional
        The desired level of verboseness in the log statements displayed on the screen and written to the
        .log file. Default value is 'INFO'.

    plot_output_dest : str, optional
        Destination to direct plots, 'screen' simply displays them to the screen. 'file' writes the plots and
        statistics to a single multi-page .pdf file, and 'none' option turns off all plot generation. Default
        value is "none".

    Returns
    -------
    Nothing!
    """
    log.setLevel(log_level)
    if plot_output_dest not in ['file', 'none', 'screen']:
        errmsg = "'{}' is not a valid input for argument 'plot_output_dest'. Valid inputs are 'file', 'none', or 'screen'.".format(plot_output_dest)
        log.error(errmsg)
        raise ValueError(errmsg)
    # 0: read in flc/flt fits files from user-specified fits file
    with open(flcflt_list, mode='r') as imgfile:
        imglist = imgfile.readlines()
    for x in range(0, len(imglist)):
        imglist[x] = imglist[x].strip()

    # 1: generate WCS obj. for custom mosaic image
    mosaic_wcs = stwcs.wcsutil.HSTWCS(mosaic_imgname, ext=1)

    # 2a: generate table of all gaia sources in frame
    gaia_table = amutils.create_astrometric_catalog(imglist, existing_wcs=mosaic_wcs,
                                                    catalog='GAIAedr3', use_footprint=True)

    # 2b: Remove gaia sources outside footprint of input flc/flt images, add X and Y coord columns
    mosaic_hdu = fits.open(mosaic_imgname)
    x, y = mosaic_wcs.all_world2pix(gaia_table['RA'], gaia_table['DEC'], 0)
    x_col = Column(name="X", data=x, dtype=np.float64)
    y_col = Column(name="Y", data=y, dtype=np.float64)
    gaia_table.add_columns([x_col, y_col], indexes=[0, 0])
    footprint = cell_utils.SkyFootprint(meta_wcs=mosaic_wcs)
    footprint.build(imglist)
    gaia_mask_array = np.where(footprint.total_mask == 0, np.nan, footprint.total_mask)
    if diagnostic_mode:
        out_mask_fitsname = "gaia_mask_array.fits"
        foo = footprint.get_footprint_hdu(filename=out_mask_fitsname)
        log.info("Wrote " + out_mask_fitsname)
    mask = amutils.within_footprint(gaia_mask_array, mosaic_wcs, x, y)
    gaia_table = gaia_table[mask]
    if diagnostic_mode:
        write_region_file("gaia_edr3_trimmed.reg", gaia_table, ['RA', 'DEC'], log_level=log_level)

    # 3: feed x, y coords into photutils.detection.daostarfinder() as initial guesses to get actual centroid
    # positions of gaia sources
    # create mask image for source detection. Pixels with value of "0" are to processed, and those with value
    # of "1" will be omitted from processing.
    dao_mask_array = np.where(footprint.total_mask == 0, 1, 0)
    xy_gaia_coords = Table([gaia_table['X'].data.astype(np.int64),
                            gaia_table['Y'].data.astype(np.int64)], names=('x_peak', 'y_peak'))
    # the below line computes a FWHM value based on detected sources (not the gaia sources). The FWHM value
    # doesn't kick out a lot of sources.
    mpeaks, mfwhm = decutils.find_point_sources(mosaic_imgname, mask=np.invert(dao_mask_array),
                                                def_fwhm=3.0, box_size=7, block_size=(1024, 1024),
                                                diagnostic_mode=False)
    # mfwhm = 25.0 # If it fails due to a lack of sources, use mfwhm = 25.0 instead.
    daofind = decutils.UserStarFinder(fwhm=mfwhm, threshold=0.0, coords=xy_gaia_coords,
                                      sharplo=0.4, sharphi=0.9)
    detection_table = daofind(mosaic_hdu["SCI"].data, mask=dao_mask_array)
    detection_table.rename_column('xcentroid', 'X')
    detection_table.rename_column('ycentroid', 'Y')
    n_detection = len(detection_table)
    n_gaia = len(gaia_table)
    pct_detection = 100.0 * (float(n_detection) / float(n_gaia))
    log.info("Found {} peaks from {} GAIA source(s)".format(n_detection, n_gaia))
    log.info("{}% of GAIA sources detected".format(pct_detection))

    # 4: convert daostarfinder output x, y centroid positions to RA, DEC using step 1 WCS info
    ra, dec = mosaic_wcs.all_pix2world(detection_table['X'], detection_table['Y'], 0)
    ra_col = Column(name="RA", data=ra, dtype=np.float64)
    dec_col = Column(name="DEC", data=dec, dtype=np.float64)
    detection_table.add_columns([ra_col, dec_col], indexes=[3, 3])
    if diagnostic_mode:
        write_region_file("test_detection.reg", detection_table, ['RA', 'DEC'], log_level=log_level)

    # 5: Identify and isolate X, Y, RA and DEC values common to both the gaia and detection tables.
    # 5a: find sources common to both the gaia table and the detection table
    try:
        coo_prefix_string = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
        gaia_coo_filename = "{}_gaia.coo".format(coo_prefix_string)
        det_coo_filename = "{}_det.coo".format(coo_prefix_string)
        write_region_file(gaia_coo_filename, gaia_table, ['X', 'Y'], verbose=False)
        write_region_file(det_coo_filename, detection_table, ['X', 'Y'], verbose=False)
        matches_gaia_to_det, matches_det_to_gaia = cu.getMatchedLists([gaia_coo_filename, det_coo_filename],
                                                                      [mosaic_imgname, mosaic_imgname],
                                                                      [n_gaia, n_detection],
                                                                      log_level)
        if len(matches_gaia_to_det) == 0:
            err_msg = "Error: No matching sources found."
            log.error(err_msg)
            raise Exception(err_msg)
    finally:
        for item in [det_coo_filename, gaia_coo_filename]:
            if os.path.exists(item):
                log.debug("Removing temp coord file {}".format(item))
                os.remove(item)

    # 5b: Isolate sources common to both the gaia table and the detection table
    matched_values_dict = {}
    for col_title in ['X', 'Y', 'RA', 'DEC']:
        matched_values_dict[col_title] = cu.extractMatchedLines(col_title, gaia_table, detection_table,
                                                                matches_gaia_to_det, matches_det_to_gaia)
    if diagnostic_mode:
        csl.check_match_quality(matched_values_dict['X'], matched_values_dict['Y'])

    # 6: compute and report statistics based on X, Y and RA, DEC position residuals.
    if plot_output_dest == "file":
        pdf_file_list = []
    plotfile_prefix = "{}_gaia".format(fits.getval(imglist[0], "FILENAME")[-17:-11])
    test_result_dict = {}

    # 6a: compute statistics on X residuals of matched sources
    rt_status, pdf_files = csl.computeLinearStats(matched_values_dict['X'], 0.1, "Pixels", plot_output_dest,
                                                  "X Axis Residuals", plotfile_prefix, ['GAIA', 'DETECTION'],
                                                  True, log_level=log_level)
    test_result_dict["X Axis Residuals"] = rt_status
    if plot_output_dest == "file":
        pdf_file_list += pdf_files

    # 6b: compute statistics on Y residuals of matched sources
    rt_status, pdf_files = csl.computeLinearStats(matched_values_dict['Y'], 0.1, "Pixels", plot_output_dest,
                                                  "Y Axis Residuals", plotfile_prefix, ['GAIA', 'DETECTION'],
                                                  True, log_level=log_level)
    test_result_dict["Y Axis Residuals"] = rt_status
    if plot_output_dest == "file":
        pdf_file_list += pdf_files
    # 6c: optionally generate X Y residuals vector plot
    if plot_output_dest in ['screen', 'file']:
        pdf_files = csl.makeVectorPlot(matched_values_dict['X'], matched_values_dict['Y'], mosaic_wcs.pscale,
                                       plot_output_dest, plotfile_prefix, ['GAIA', 'DETECTION'])
        if plot_output_dest == "file":
            pdf_file_list += [pdf_files]
    # 6d: compute statistics on RA/DEC residuals of matched sources
    # convert reference and comparison RA/Dec values into SkyCoord objects
    img_coord_sys = mosaic_hdu['SCI'].header['radesys'].lower()
    matched_values_ref = SkyCoord(matched_values_dict['RA'][0, :], matched_values_dict['DEC'][0, :],
                                  frame=img_coord_sys, unit="deg")
    matched_values_comp = SkyCoord(matched_values_dict['RA'][1, :], matched_values_dict['DEC'][1, :],
                                   frame=img_coord_sys, unit="deg")
    # convert to ICRS coord system if need be
    if img_coord_sys != "icrs":
        matched_values_ref = matched_values_ref.icrs
        matched_values_comp = matched_values_comp.icrs

    matched_values = [matched_values_ref, matched_values_comp]
    rt_status, pdf_files = csl.computeLinearStats(matched_values, 0.1, "arcseconds", plot_output_dest,
                                                  "On-Sky Separation", plotfile_prefix, ['GAIA', 'DETECTION'],
                                                  True, log_level=log_level)
    test_result_dict["On-Sky Separation"] = rt_status
    if plot_output_dest == "file":
        pdf_file_list += pdf_files

    # 7: Report results
    log_output_string_list = []
    len_list = []
    col_titles = ["X Axis Residuals", "Y Axis Residuals", "On-Sky Separation"]
    for item in col_titles:
        len_list.append(len(item))
    total_padded_size = max(len_list) + 3
    log_output_string_list.append("{}{}".format(" " * 36, "SUMMARY OF RESULTS"))
    log_output_string_list.append("-" * (70 + total_padded_size))
    log_output_string_list.append("{}{}".format(" " * (total_padded_size + 46), "% within     % beyond"))
    log_output_string_list.append(
        "COLUMN{}STATUS   MEAN        MEDIAN       STD DEV     3\u03C3 of mean   3\u03C3 of mean".format(
            " " * (total_padded_size - 6)))
    overall_status = "OK"
    for colTitle in col_titles:
        log_output_string_list.append(
            "%s%s%s" % (colTitle, "." * (total_padded_size - len(colTitle)), test_result_dict[colTitle]))
        if not test_result_dict[colTitle].startswith("OK"):
            overall_status = "FAILURE"
    log_output_string_list.append("-" * (70 + total_padded_size))
    log_output_string_list.append("OVERALL TEST STATUS{}{}".format("." * (total_padded_size - 19),
                                                                   overall_status))
    for log_line in log_output_string_list:
        log.info(log_line)

    if plot_output_dest == "file":
        # generate final overall summary pdf page
        stat_summary_file_name = "stats_summary.pdf"
        if plotfile_prefix is not None:
            stat_summary_file_name = "{}_{}".format(plotfile_prefix, stat_summary_file_name)
        fig = plt.figure(figsize=(11, 8.5))
        fig.text(0.5, 0.87, "Summary of Results", transform=fig.transFigure, size=12, ha="center")
        stat_text_blob = ""
        for log_line in log_output_string_list:
            if log_line != "\n":
                stat_text_blob += log_line + "\n"
            else:
                stat_text_blob += "\n"

        stat_text_blob += "\n\nGenerated {}\n".format(datetime.now().strftime("%m/%d/%Y %H:%M:%S"))
        stat_text_blob = csl.wrap_long_filenames_on_stats_page(stat_text_blob, ['GAIA', 'DETECTION'])
        fig.text(0.5, 0.5, stat_text_blob, transform=fig.transFigure, size=10, ha="center", va="center",
                 multialignment="left", family="monospace")
        fig.savefig(stat_summary_file_name)
        plt.close()
        pdf_file_list.append(stat_summary_file_name)
        # combine all individual plot files into a single multiple-page pdf file
        final_plot_filename = "residual_plots.pdf"
        if plotfile_prefix:
            final_plot_filename = "{}_{}".format(plotfile_prefix, final_plot_filename)
        csl.pdf_merger(final_plot_filename, pdf_file_list)
        log.info("Sourcelist comparison plots saved to file {}.".format(final_plot_filename))
# ============================================================================================================


def write_region_file(filename, table_data, colnames, apply_zero_index_correction=False, log_level=logutil.logging.INFO, verbose=True):
    """Write out columns from user-specified table to ds9 region file

    Parameters
    ----------
    filename : str
        name of the output region file to be created

    table_data : astropy.Table
        Table continaing values to be written out

    colnames : list
        list of the columns from table_data to write out

    apply_zero_index_correction : Bool, optional
        Add 1 to all X and Y values to make them 1-indexed if they were initially zero indexed. Default
        value is Boolean 'False'.

    log_level : int, optional
        The desired level of verboseness in the log statements displayed on the screen and written to the
        .log file. Default value is 'INFO'.

    verbose : Bool, optional
        Print confirmation? Default value is Boolean 'False'.

    Returns
    -------
    Nothing.
    """
    log.setLevel(log_level)
    xcolname = colnames[0]
    ycolname = colnames[1]

    data_table = table_data.copy()
    out_data = data_table[xcolname, ycolname]

    if apply_zero_index_correction:
        log.info("Added 1-pixel offset to X and Y output values to adjust from the 0-indexed coordinate "
                 "system of the data table to the 1-indexed coordinate system assumed for ds9 .reg files.")
        out_data[xcolname] += 1.0
        out_data[ycolname] += 1.0

    out_data.write(filename, format='ascii.fast_no_header', overwrite=True)
    if verbose and log_level <= 20:
        log.info("Wrote " + filename)


# ============================================================================================================


if __name__ == "__main__":

    log_level_dict = {"critical": logutil.logging.CRITICAL,
                      "error": logutil.logging.ERROR,
                      "warning": logutil.logging.WARNING,
                      "info": logutil.logging.INFO,
                      "debug": logutil.logging.DEBUG}
    # Parse command-line input args
    parser = argparse.ArgumentParser(description='Statistically quantify quality of GAIA MVM alignment')
    parser.add_argument('mosaic_imgname', help='Name of the MVM-processed mosaic image to process')
    parser.add_argument('flcflt_list', help='list of calibrated flc.fits and/or flt.fits images to process')
    parser.add_argument('-d', '--diagnostic_mode', required=False, action='store_true',
                        help='If this option is turned on, additional log messages will be displayed and '
                             'additional files will be created during the course of the run.')
    parser.add_argument('-l', '--log_level', required=False, default='info',
                        choices=['critical', 'error', 'warning', 'info', 'debug'],
                        help='The desired level of verboseness in the log statements displayed on the screen '
                        'and written to the .log file. The level of verboseness from left to right, and '
                        'includes all log statements with a log_level left of the specified level. '
                        'Specifying "critical" will only record/display "critical" log statements, and '
                        'specifying "error" will record/display both "error" and "critical" log statements, '
                        'and so on.')
    parser.add_argument('-p', '--plot_output_dest', required=False, default='none',
                        choices=['file', 'none', 'screen'],
                        help='Destination to direct plots, "screen" simply displays them to the screen. '
                        '"file" writes the plots and statistics to a single multi-page .pdf file, and "none" '
                        'option turns off all plot generation. Default value is "none".')
    input_args = parser.parse_args()

    # Perform analysis
    perform(input_args.mosaic_imgname, input_args.flcflt_list, diagnostic_mode=input_args.diagnostic_mode,
            log_level=log_level_dict[input_args.log_level], plot_output_dest=input_args.plot_output_dest)
