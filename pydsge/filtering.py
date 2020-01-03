#!/bin/python
# -*- coding: utf-8 -*-

import numpy as np
from .core import time
from grgrlib.core import timeprint
from econsieve.stats import logpdf


def create_obs_cov(self, scale_obs=0.1):

    self.Z = np.array(self.data)
    sig_obs = np.var(self.Z, axis=0)*scale_obs**2
    obs_cov = np.diagflat(sig_obs)

    return obs_cov


def create_filter(self, P=None, R=None, N=None, ftype=None, seed=None, **fargs):

    self.Z = np.array(self.data)

    if ftype == 'KalmanFilter' or ftype == 'KF':

        from econsieve import KalmanFilter

        f = KalmanFilter(dim_x=len(self.vv), dim_z=self.ny)
        f.F = self.linear_representation
        f.H = self.hx

    elif ftype in ('PF', 'APF', 'ParticleFilter', 'AuxiliaryParticleFilter'):

        from .partfilt import ParticleFilter

        if N is None:
            N = 10000

        aux_bs = ftype in ('AuxiliaryParticleFilter', 'APF')
        f = ParticleFilter(N=N, dim_x=len(self.vv),
                           dim_z=self.ny, auxiliary_bootstrap=aux_bs)

    else:

        from econsieve import TEnKF

        if N is None:
            N = 500
        f = TEnKF(N=N, dim_x=len(self.vv), dim_z=self.ny, seed=seed, **fargs)

    if P is not None:
        f.P = P
    elif hasattr(self, 'P'):
        f.P = self.P
    else:
        f.P *= 1e1
    f.init_P = f.P

    if R is not None:
        f.R = R

    f.eps_cov = self.QQ(self.ppar)
    f.Q = self.QQ(self.ppar) @ self.QQ(self.ppar)

    if ftype in ('KalmanFilter', 'KF'):
        CO = self.SIG @ f.eps_cov
        f.Q = CO @ CO.T

    self.filter = f

    return f


def get_ll(self, **args):
    return run_filter(self, smoother=False, get_ll=True, **args)


def run_filter(self, smoother=True, get_ll=False, dispatch=None, rcond=1e-14, constr_data=None, verbose=False):

    if verbose:
        st = time.time()

    if constr_data is None:
        if self.filter.name == 'ParticleFilter':
            constr_data = 'elb_level'  # wild guess
        else:
            constr_data = False

    if constr_data:
        # copy the data
        data = self.data
        # constaint const_obs
        x_shift = self.get_par(constr_data)
        data[str(self.const_obs)] = np.maximum(
            data[str(self.const_obs)], x_shift)
        # send to filter
        self.Z = np.array(data)
    else:
        self.Z = np.array(self.data)

    if dispatch is None:
        dispatch = self.filter.name == 'ParticleFilter'

    if dispatch:
        from .engine import func_dispatch
        t_func_jit, o_func_jit, get_eps_jit = func_dispatch(self, full=True)
        self.filter.t_func = t_func_jit
        self.filter.o_func = o_func_jit
        self.filter.get_eps = get_eps_jit

    else:
        self.filter.t_func = self.t_func
        self.filter.o_func = self.o_func
        self.filter.get_eps = self.get_eps_lin

    if self.filter.name == 'KalmanFilter':

        res, covs, ll = self.filter.batch_filter(self.Z)

        if get_ll:
            res = ll

        if smoother:
            res, covs, _, _ = self.filter.rts_smoother(
                res, covs, inv=np.linalg.pinv)

        self.covs = covs

    elif self.filter.name == 'ParticleFilter':

        res = self.filter.batch_filter(self.Z)

        if smoother:

            if verbose:
                print('[run_filter:]'.ljust(
                    15, ' ')+'Filtering done after %s seconds, starting smoothing...' % np.round(time.time()-st, 3))

            if isinstance(smoother, bool):
                smoother = 10
            res = self.filter.smoother(smoother)

    else:

        res = self.filter.batch_filter(
            self.Z, calc_ll=get_ll, store=smoother, verbose=verbose)

        if smoother:
            res = self.filter.rts_smoother(res, rcond=rcond)

    if get_ll:
        if np.isnan(res):
            res = -np.inf
        self.ll = res

        if verbose:
            print('[run_filter:]'.ljust(15, ' ')+'Filtering done in %s. Likelihood is %s.' %
                  (timeprint(time.time()-st, 3), res))
    else:
        self.X = res

        if verbose:
            print('[run_filter:]'.ljust(15, ' ')+'Filtering done in %s.' %
                  timeprint(time.time()-st, 3))

    return res


def extract(self, sample=None, nsamples=1, precalc=True, seed=0, store_path=None, verbose=True, debug=False, **npasargs):
    """Extract the timeseries of (smoothed) shocks.

    Parameters
    ----------
    sample : array, optional
        Provide one or several parameter vectors used for which the smoothed shocks are calculated (default is the current `self.par`)
    nsamples : int, optional
        Number of `npas`-draws for each element in `sample`. Defaults to 1

    Returns
    -------
    tuple
        The result(s)
    """

    import tqdm
    import os
    from grgrlib.core import map2arr, serializer

    self.debug |= debug

    if np.ndim(sample) <= 1:
        sample = [sample]

    sample = [(x, y) for x in sample for y in range(nsamples)]

    run_filter = serializer(self.run_filter)
    set_par = serializer(self.set_par)
    npas = serializer(self.filter.npas)
    filter_get_eps = serializer(self.filter.get_eps)

    def runner(arg):

        par, seed_loc = arg

        if par is not None:
            set_par(par)

        run_filter(verbose=False)

        get_eps = filter_get_eps if precalc else None

        for natt in range(4):
            try:
                means, covs, resid, flags = npas(
                    get_eps=get_eps, verbose=verbose-1, seed=seed_loc, nsamples=1, **npasargs)

                return means[0], covs, resid[0], flags
            except:
                if natt < 3:
                    pass
                else:
                    raise

    wrap = tqdm.tqdm if verbose else (lambda x, **kwarg: x)
    res = wrap(self.mapper(runner, sample), unit=' sample(s)',
               total=len(sample), dynamic_ncols=True)
    means, covs, resid, flags = map2arr(res)

    if self.pool:
        self.pool.close()

    edict = {'pars': [s[0] for s in sample],
             'means': means,
             'covs': covs,
             'resid': resid,
             'flags': flags}

    if store_path:

        if not isinstance(store_path, str):
            store_path = self.name + '_eps'

        if store_path[-4] == '.npz':
            store_path = store_path[-4]

        if not os.path.isabs(store_path):
            store_path = os.path.join(self.path, store_path)

        np.savez(store_path, **edict)

    self.eps_dict = edict

    return edict
