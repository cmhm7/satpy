#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2020 Satpy developers
#
# This file is part of satpy.
#
# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.
"""FIDUCEO MVIRI FCDR Reader.

Introduction
------------
The FIDUCEO MVIRI FCDR is a Fundamental Climate Data Record (FCDR) of
re-calibrated Level 1.5 Infrared, Water Vapour, and Visible radiances from
the Meteosat Visible Infra-Red Imager (MVIRI) instrument onboard the
Meteosat First Generation satellites. There are two variants of the dataset:
The *full FCDR* and a simplified version called *easy FCDR*. Some datasets are
only available in one of the two variants, see the corresponding YAML
definition in ``satpy/etc/readers/``.

Dataset Names
-------------
The FIDUCEO MVIRI readers use names ``VIS``, ``WV`` and ``IR`` for the visible,
water vapor and infrared channels, respectively. These are different from
the original netCDF variable names for the following reasons:

- VIS channel is named differently in full FCDR (``counts_vis``) and easy FCDR
  (``toa_bidirectional_reflectance_vis``)
- netCDF variable names contain the calibration level (e.g. ``counts_...``),
  which might be confusing for satpy users if a different calibration level
  is chosen.

Remaining datasets (such as quality flags and uncertainties) have the same
name in the reader as in the netCDF file.


Example
-------
This is how to read FIDUCEO MVIRI FCDR data in satpy:

.. code-block:: python

    from satpy import Scene

    scn = Scene(filenames=['FIDUCEO_FCDR_L15_MVIRI_MET7-57.0...'],
                reader='mviri_l1b_fiduceo_nc')
    scn.load(['VIS', 'WV', 'IR'])

Global netCDF attributes are available in the ``raw_metadata`` attribute of
each loaded dataset.


Image Orientation
-----------------
The images are stored in MVIRI scanning direction, that means South is up and
East is right. This can be changed as follows:

.. code-block:: python

    scn.load(['VIS'], upper_right_corner='NE')


Geolocation
-----------
In addition to the image data, FIDUCEO also provides so called *static FCDRs*
containing latitude and longitude coordinates. In order to simplify their
usage, the FIDUCEO MVIRI readers do not make use of these static files, but
instead provide an area definition that can be used to compute longitude and
latitude coordinates on demand.

.. code-block:: python

    area = scn['VIS'].attrs['area']
    lons, lats = area.get_lonlats()

Those were compared to the static FCDR and they agree very well, however there
are small differences. The mean difference is < 1E3 degrees for all channels
and projection longitudes.


Huge VIS Reflectances
---------------------
You might encounter huge VIS reflectances (10^8 percent and greater) in
situations where both radiance and solar zenith angle are small. The reader
certainly needs some improvement in this regard. Maybe the corresponding
uncertainties can be used to filter these cases before calculating reflectances.


VIS Channel Quality Flags
-------------------------
Quality flags are available for the VIS channel only. A simple approach for
masking bad quality pixels is to set the ``mask_bad_quality`` keyword argument
to ``True``:

.. code-block:: python

    scn = Scene(filenames=['FIDUCEO_FCDR_L15_MVIRI_MET7-57.0...'],
                reader='mviri_l1b_fiduceo_nc',
                reader_kwargs={'mask_bad_quality': True})

See :class:`FiduceoMviriBase` for an argument description. In some situations
however the entire image can be flagged (look out for warnings). In that case
check out the ``quality_pixel_bitmask`` and ``data_quality_bitmask`` datasets
to find out why.


Angles
------
The FIDUCEO MVIRI FCDR provides satellite and solar angles on a coarse tiepoint
grid. By default these datasets will be interpolated to the higher VIS
resolution. This can be changed as follows:

.. code-block:: python

    scn.load(['solar_zenith_angle'], resolution=4500)

If you need the angles in both resolutions, use data queries:

.. code-block:: python

    from satpy import DataQuery

    query_vis = DataQuery(
        name='solar_zenith_angle',
        resolution=2250
    )
    query_ir = DataQuery(
        name='solar_zenith_angle',
        resolution=4500
    )
    scn.load([query_vis, query_ir])

    # Use the query objects to access the datasets as follows
    sza_vis = scn[query_vis]


References
----------
    - `[Handbook]`_ MFG User Handbook
    - `[PUG]`_ FIDUCEO MVIRI FCDR Product User Guide

.. _[Handbook]: http://www.eumetsat.int/\
website/wcm/idc/idcplg?IdcService=GET_FILE&dDocName=PDF_TD06_MARF&\
RevisionSelectionMethod=LatestReleased&Rendition=Web
.. _[PUG]: http://doi.org/10.15770/EUM_SEC_CLM_0009
"""

import abc
from functools import lru_cache
import warnings

import dask.array as da
import numpy as np
import xarray as xr

from satpy import CHUNK_SIZE
from satpy.readers._geos_area import (
    sampling_to_lfac_cfac,
    get_area_definition,
    get_area_extent
)
from satpy.readers.file_handlers import BaseFileHandler

EQUATOR_RADIUS = 6378140.0
POLE_RADIUS = 6356755.0
ALTITUDE = 42164000.0 - EQUATOR_RADIUS
"""[Handbook] section 5.2.1."""

MVIRI_FIELD_OF_VIEW = 18.0
"""[Handbook] section 5.3.2.1."""

CHANNELS = ['VIS', 'WV', 'IR']
ANGLES = [
    'solar_zenith_angle',
    'solar_azimuth_angle',
    'satellite_zenith_angle',
    'satellite_azimuth_angle'
]
OTHER_REFLECTANCES = [
    'u_independent_toa_bidirectional_reflectance',
    'u_structured_toa_bidirectional_reflectance'
]
HIGH_RESOL = 2250


class FiduceoMviriBase(BaseFileHandler):
    """Baseclass for FIDUCEO MVIRI file handlers."""
    nc_keys = {
        'WV': 'count_wv',
        'IR': 'count_ir'
    }
    nc_keys_coefs = {
        'WV': {
            'radiance': {
                'a': 'a_wv',
                'b': 'b_wv'
            },
            'brightness_temperature': {
                'a': 'bt_a_wv',
                'b': 'bt_b_wv'
            }
        },
        'IR': {
            'radiance': {
                'a': 'a_ir',
                'b': 'b_ir'
            },
            'brightness_temperature': {
                'a': 'bt_a_ir',
                'b': 'bt_b_ir'
            }
        },
    }

    def __init__(self, filename, filename_info, filetype_info,
                 mask_bad_quality=False):
        """Initialize the file handler.

        Args:
             mask_bad_quality: Mask VIS pixels with bad quality, that means
                 any quality flag except "ok". If you need more control, use
                 the ``quality_pixel_bitmask`` and ``data_quality_bitmask``
                 datasets.
        """
        super(FiduceoMviriBase, self).__init__(
            filename, filename_info, filetype_info)
        self.mask_bad_quality = mask_bad_quality
        self.nc = xr.open_dataset(
            filename,
            chunks={'x': CHUNK_SIZE,
                    'y': CHUNK_SIZE,
                    'x_ir_wv': CHUNK_SIZE,
                    'y_ir_wv': CHUNK_SIZE}
        )

        # Projection longitude is not provided in the file, read it from the
        # filename.
        self.projection_longitude = float(filename_info['projection_longitude'])

    def get_dataset(self, dataset_id, dataset_info):
        """Get the dataset."""
        name = dataset_id['name']
        resolution = dataset_id['resolution']
        if name in ANGLES:
            ds = self._get_angles(name, resolution)
        elif name in CHANNELS:
            ds = self._get_channel(name, resolution, dataset_id['calibration'])
        else:
            ds = self._get_other_dataset(name)
        self._update_attrs(ds, dataset_info)
        return ds

    def get_area_def(self, dataset_id):
        """Get area definition of the given dataset."""
        if self._is_high_resol(dataset_id['resolution']):
            im_size = self.nc.coords['y'].size
            area_name = 'geos_mviri_vis'
        else:
            im_size = self.nc.coords['y_ir_wv'].size
            area_name = 'geos_mviri_ir_wv'

        # Determine line/column offsets and scaling factors. For offsets
        # see variables "asamp" and "aline" of subroutine "refgeo" in
        # [Handbook] and in
        # https://github.com/FIDUCEO/FCDR_MVIRI/blob/master/lib/nrCrunch/cruncher.f
        loff = coff = im_size / 2 + 0.5
        lfac = cfac = sampling_to_lfac_cfac(
            np.deg2rad(MVIRI_FIELD_OF_VIEW) / im_size
        )

        pdict = {
            'ssp_lon': self.projection_longitude,
            'a': EQUATOR_RADIUS,
            'b': POLE_RADIUS,
            'h': ALTITUDE,
            'units': 'm',
            'loff': loff - im_size,
            'coff': coff,
            'lfac': -lfac,
            'cfac': -cfac,
            'nlines': im_size,
            'ncols': im_size,
            'scandir': 'S2N',  # Reference: [PUG] section 2.
            'p_id': area_name,
            'a_name': area_name,
            'a_desc': 'MVIRI Geostationary Projection'
        }
        extent = get_area_extent(pdict)
        area_def = get_area_definition(pdict, extent)
        return area_def

    def _get_channel(self, name, resolution, calibration):
        """Get and calibrate channel data."""
        ds = self._read_dataset(name)
        ds = self._calibrate(
            ds,
            channel=name,
            calibration=calibration
        )
        if name == 'VIS':
            if self.mask_bad_quality:
                ds = self._mask_vis(ds)
            else:
                self._check_vis_quality(ds)
        ds['acq_time'] = ('y', self._get_acq_time(resolution))
        return ds

    @lru_cache(maxsize=8)  # 4 angle datasets with two resolutions each
    def _get_angles(self, name, resolution):
        """Get angle dataset.

        Files provide angles (solar/satellite zenith & azimuth) at a coarser
        resolution. Interpolate them to the desired resolution.
        """
        angles = self._read_dataset(name)
        if self._is_high_resol(resolution):
            target_x = self.nc.coords['x']
            target_y = self.nc.coords['y']
        else:
            target_x = self.nc.coords['x_ir_wv']
            target_y = self.nc.coords['y_ir_wv']
        return self._interp_tiepoints(
            angles,
            target_x=target_x,
            target_y=target_y
        )

    def _get_other_dataset(self, name):
        """Get other datasets such as uncertainties."""
        ds = self._read_dataset(name)
        if name in OTHER_REFLECTANCES:
            ds = ds * 100  # conversion to percent
        return ds

    def _get_nc_key(self, ds_name):
        """Get netCDF variable name for the given dataset."""
        return self.nc_keys.get(ds_name, ds_name)

    def _read_dataset(self, name):
        """Read a dataset from the file."""
        nc_key = self._get_nc_key(name)
        ds = self.nc[nc_key]
        if 'y_ir_wv' in ds.dims:
            ds = ds.rename({'y_ir_wv': 'y', 'x_ir_wv': 'x'})
        elif 'y_tie' in ds.dims:
            ds = ds.rename({'y_tie': 'y', 'x_tie': 'x'})
        elif 'y' in ds.dims and 'y' not in ds.coords:
            # For some reason xarray doesn't assign coordinates to all
            # high resolution data variables.
            ds = ds.assign_coords({'y': self.nc.coords['y'],
                                   'x': self.nc.coords['x']})
        return ds

    def _update_attrs(self, ds, info):
        """Update dataset attributes."""
        ds.attrs.update(info)
        ds.attrs.update({'platform': self.filename_info['platform'],
                         'sensor': self.filename_info['sensor']})
        ds.attrs['raw_metadata'] = self.nc.attrs
        ds.attrs['orbital_parameters'] = self._get_orbital_parameters()

    def _calibrate(self, ds, channel, calibration):
        """Calibrate the given dataset."""
        ds.attrs.pop('ancillary_variables', None)  # to avoid satpy warnings
        if channel == 'VIS':
            return self._calibrate_vis(ds, calibration)
        elif channel in ['WV', 'IR']:
            return self._calibrate_ir_wv(ds, channel, calibration)
        else:
            raise KeyError('Don\'t know how to calibrate channel {}'.format(
                channel))

    @abc.abstractmethod
    def _calibrate_vis(self, ds, calibration):
        """Calibrate VIS channel. To be implemented by subclasses."""
        raise NotImplementedError

    def _update_refl_attrs(self, refl):
        """Update attributes of reflectance datasets."""
        refl.attrs['sun_earth_distance_correction_applied'] = True
        refl.attrs['sun_earth_distance_correction_factor'] = self.nc[
            'distance_sun_earth'].item()
        return refl

    def _calibrate_ir_wv(self, ds, channel, calibration):
        """Calibrate IR and WV channel."""
        if calibration == 'counts':
            return ds
        elif calibration in ('radiance', 'brightness_temperature'):
            rad = self._ir_wv_counts_to_radiance(ds, channel)
            if calibration == 'radiance':
                return rad
            bt = self._ir_wv_radiance_to_brightness_temperature(rad, channel)
            return bt
        else:
            raise KeyError('Invalid calibration: {}'.format(calibration.name))

    def _get_coefs_ir_wv(self, channel, calibration):
        """Get calibration coefficients for IR/WV channels.

        Returns:
            Offset (a), Slope (b)
        """
        nc_key_a = self.nc_keys_coefs[channel][calibration]['a']
        nc_key_b = self.nc_keys_coefs[channel][calibration]['b']
        a = np.float32(self.nc[nc_key_a])
        b = np.float32(self.nc[nc_key_b])
        return a, b

    def _ir_wv_counts_to_radiance(self, counts, channel):
        """Convert IR/WV counts to radiance.

        Reference: [PUG], equations (4.1) and (4.2).
        """
        a, b = self._get_coefs_ir_wv(channel, 'radiance')
        rad = a + b * counts
        return rad.where(rad > 0, np.float32(np.nan))

    def _ir_wv_radiance_to_brightness_temperature(self, rad, channel):
        """Convert IR/WV radiance to brightness temperature.

        Reference: [PUG], equations (5.1) and (5.2).
        """
        a, b = self._get_coefs_ir_wv(channel, 'brightness_temperature')
        bt = b / (np.log(rad) - a)
        return bt.where(bt > 0, np.float32(np.nan))

    def _check_vis_quality(self, ds):
        """Check VIS channel quality and issue a warning if it's bad."""
        mask = self._read_dataset('quality_pixel_bitmask')
        use_with_caution = da.bitwise_and(mask, 2)
        if use_with_caution.all():
            warnings.warn(
                'All pixels of the VIS channel are flagged as "use with '
                'caution". Use datasets "quality_pixel_bitmask" and '
                '"data_quality_bitmask" to find out why.'
            )

    def _mask_vis(self, ds):
        """Mask VIS pixels with bad quality.

        Pixels are considered bad quality if the "quality_pixel_bitmask" is
        everything else than 0 (no flag set).
        """
        mask = self._read_dataset('quality_pixel_bitmask')
        return ds.where(mask == 0,
                        np.float32(np.nan))

    @lru_cache(maxsize=3)  # Three channels
    def _get_acq_time(self, resolution):
        """Get scanline acquisition time for the given resolution.

        Note that the acquisition time does not increase monotonically
        with the scanline number due to the scan pattern and rectification.
        """
        # Variable is sometimes named "time" and sometimes "time_ir_wv".
        try:
            time2d = self.nc['time_ir_wv']
        except KeyError:
            time2d = self.nc['time']
        if self._is_high_resol(resolution):
            target_y = self.nc.coords['x']
        else:
            target_y = self.nc.coords['x_ir_wv']
        return self._interp_acq_time(time2d, target_y=target_y.values)

    def _interp_acq_time(self, time2d, target_y):
        """Interpolate scanline acquisition time to the given coordinates.

        The files provide timestamps per pixel for the low resolution
        channels (IR/WV) only.

        1) Average values in each line to obtain one timestamp per line.
        2) For the VIS channel duplicate values in y-direction (as
           advised by [PUG]).

        Note that the timestamps do not increase monotonically with the
        line number in some cases.

        Returns:
            Mean scanline acquisition timestamps
        """
        # Compute mean timestamp per scanline
        time = time2d.mean(dim='x_ir_wv').rename({'y_ir_wv': 'y'})

        # If required, repeat timestamps in y-direction to obtain higher
        # resolution
        y = time.coords['y'].values
        if y.size < target_y.size:
            reps = target_y.size // y.size
            y_rep = np.repeat(y, reps)
            time_hires = time.reindex(y=y_rep)
            time_hires = time_hires.assign_coords(y=target_y)
            return time_hires
        return time

    def _get_orbital_parameters(self):
        """Get the orbital parameters."""
        orbital_parameters = {
            'projection_longitude': self.projection_longitude,
            'projection_latitude': 0.0,
            'projection_altitude': ALTITUDE
        }
        ssp_lon, ssp_lat = self._get_ssp_lonlat()
        if not np.isnan(ssp_lon) and not np.isnan(ssp_lat):
            orbital_parameters.update({
                'satellite_actual_longitude': ssp_lon,
                'satellite_actual_latitude': ssp_lat,
                # altitude not available
            })
        return orbital_parameters

    def _get_ssp_lonlat(self):
        """Get longitude and latitude at the subsatellite point.

        Easy FCDR files provide satellite position at the beginning and
        end of the scan. This method computes the mean of those two values.
        In the full FCDR the information seems to be missing.

        Returns:
            Subsatellite longitude and latitude
        """
        ssp_lon = self._get_ssp('longitude')
        ssp_lat = self._get_ssp('latitude')
        return ssp_lon, ssp_lat

    def _get_ssp(self, coord):
        key_start = 'sub_satellite_{}_start'.format(coord)
        key_end = 'sub_satellite_{}_end'.format(coord)
        try:
            sub_lonlat = np.nanmean(
                [self.nc[key_start].values,
                 self.nc[key_end].values]
            )
        except KeyError:
            # Variables seem to be missing in full FCDR
            sub_lonlat = np.nan
        return sub_lonlat

    def _interp_tiepoints(self, ds, target_x, target_y):
        """Interpolate dataset between tiepoints.

        Uses linear interpolation.

        FUTURE: [PUG] recommends cubic spline interpolation.

        Args:
            ds:
                Dataset to be interpolated
            target_x:
                Target x coordinates
            target_y:
                Target y coordinates
        """
        # No tiepoint coordinates specified in the files. Use dimensions
        # to calculate tiepoint sampling and assign tiepoint coordinates
        # accordingly.
        sampling = target_x.size // ds.coords['x'].size
        ds = ds.assign_coords(x=target_x.values[::sampling],
                              y=target_y.values[::sampling])

        return ds.interp(x=target_x.values, y=target_y.values)

    def _is_high_resol(self, resolution):
        return resolution == HIGH_RESOL


class FiduceoMviriEasyFcdrFileHandler(FiduceoMviriBase):
    """File handler for FIDUCEO MVIRI Easy FCDR."""

    nc_keys = FiduceoMviriBase.nc_keys.copy()
    nc_keys['VIS'] = 'toa_bidirectional_reflectance_vis'

    def _calibrate_vis(self, ds, calibration):
        """Calibrate VIS channel.

        Easy FCDR provides reflectance only, no counts or radiance.
        """
        if calibration == 'reflectance':
            refl = 100 * ds  # conversion to percent
            refl = self._update_refl_attrs(refl)
            return refl
        elif calibration in ('counts', 'radiance'):
            raise ValueError('Cannot calibrate to {}. Easy FCDR provides '
                             'reflectance only.'.format(calibration.name))
        else:
            raise KeyError('Invalid calibration: {}'.format(calibration.name))


class FiduceoMviriFullFcdrFileHandler(FiduceoMviriBase):
    """File handler for FIDUCEO MVIRI Full FCDR."""

    nc_keys = FiduceoMviriBase.nc_keys.copy()
    nc_keys['VIS'] = 'count_vis'

    def _calibrate_vis(self, ds, calibration):
        """Calibrate VIS channel.

        All calibration levels are available here.
        """
        if calibration == 'counts':
            return ds
        elif calibration in ('radiance', 'reflectance'):
            rad = self._vis_counts_to_radiance(ds)
            if calibration == 'radiance':
                return rad
            refl = self._vis_radiance_to_reflectance(rad)
            refl = self._update_refl_attrs(refl)
            return refl
        else:
            raise KeyError('Invalid calibration: {}'.format(calibration.name))

    def _vis_counts_to_radiance(self, counts):
        """Convert VIS counts to radiance.

        Reference: [PUG], equations (7) and (8).
        """
        years_since_launch = self.nc['years_since_launch']
        a_cf = (self.nc['a0_vis'] +
                self.nc['a1_vis'] * years_since_launch +
                self.nc['a2_vis'] * years_since_launch ** 2)
        mean_count_space_vis = np.float32(self.nc['mean_count_space_vis'])
        a_cf = np.float32(a_cf)
        rad = (counts - mean_count_space_vis) * a_cf
        return rad.where(rad > 0, np.float32(np.nan))

    def _vis_radiance_to_reflectance(self, rad):
        """Convert VIS radiance to reflectance factor.

        Note: Produces huge reflectances in situations where both radiance and
        solar zenith angle are small. Maybe the corresponding uncertainties
        can be used to filter these cases before calculating reflectances.

        Reference: [PUG], equation (6).
        """
        sza = self._get_angles('solar_zenith_angle', HIGH_RESOL)
        sza = sza.where(da.fabs(sza) < 90,
                        np.float32(np.nan))  # direct illumination only
        cos_sza = np.cos(np.deg2rad(sza))
        distance_sun_earth2 = np.float32(self.nc['distance_sun_earth'] ** 2)
        solar_irradiance_vis = np.float32(self.nc['solar_irradiance_vis'])
        refl = (
           (np.pi * distance_sun_earth2) /
           (solar_irradiance_vis * cos_sza) *
           rad
        )
        refl = refl * 100  # conversion to percent
        return refl
