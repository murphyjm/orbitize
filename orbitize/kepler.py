"""
This module solves for the orbit of the planet given Keplerian parameters
"""
import numpy as np
import astropy.units as u
import astropy.constants as consts
from astropy.io import fits


def calc_orbit(epochs, sma, ecc, tau, argp, lan, inc, plx, mtot, mass=0):
    """
    Returns the separation and radial velocity of the body given array of
    orbital parameters (size n_orbs) at given epochs (array of size n_dates)

    Based on orbit solvers from James Graham and Rob De Rosa. Adapted by Jason Wang.

    Args:
        epochs (np.array): MJD times for which we want the positions of the planet
        sma (np.array): semi-major axis of orbit [au]
        ecc (np.array): eccentricity of the orbit [0,1]
        tau (np.array): epoch of periastron passage in fraction of orbital period past MJD=0 [0,1]
        argp (np.array): argument of periastron [radians]
        lan (np.array): longitude of the ascending node [radians]
        inc (np.array): inclination [radians]
        plx (np.array): parallax [mas]
        mtot (np.array): total mass [Solar masses]. Note that this is
        mass (float): mass of this body [Solar masses]. For planets mass ~ 0

    Return:
        raoff (np.array): 2-D array (n_orbs x n_dates) of RA offsets between the bodies (origin is at the other body)
        deoff (np.array): 2-D array (n_orbs x n_dates) of Dec offsets between the bodies
        vz (np.array): 2-D array (n_orbs x n_dates) of radial velocity offset between the bodies

    Written: Jason Wang, Henry Ngo, 2018
    """

    n_orbs  = np.size(sma)  # num sets of input orbital parameters
    n_dates = np.size(epochs) # number of dates to compute offsets and vz

    sma = np.transpose(np.tile(sma, (n_dates, 1)))
    inc = np.transpose(np.tile(inc, (n_dates, 1)))
    ecc = np.transpose(np.tile(ecc, (n_dates, 1)))
    argp = np.transpose(np.tile(argp, (n_dates, 1)))
    lan = np.transpose(np.tile(lan, (n_dates, 1)))
    tau = np.transpose(np.tile(tau, (n_dates, 1)))
    plx = np.transpose(np.tile(plx, (n_dates, 1)))
    mtot = np.transpose(np.tile(mtot, (n_dates, 1)))
    epochs = np.tile(epochs, (n_orbs, 1))

    # Compute period (from Kepler's third law) and mean motion
    period = np.sqrt(4*np.pi**2.0*(sma*u.AU)**3/(consts.G*(mtot*u.Msun)))
    period = period.to(u.day).value
    mean_motion = 2*np.pi/(period) # in rad/day

    # compute mean anomaly (size: n_orbs x n_dates)
    manom = (mean_motion*epochs - 2*np.pi*tau) % (2.0*np.pi)
    
    # compute eccentric anomalies (size: n_orbs x n_dates)
    eanom = _calc_ecc_anom(manom, ecc)
    
    # compute the true anomalies (size: n_orbs x n_dates)
    tanom = 2.*np.arctan(np.sqrt( (1.0 + ecc)/(1.0 - ecc))*np.tan(0.5*eanom) )

    # compute 3-D orbital radius of second body (size: n_orbs x n_dates)
    radius = sma * (1.0 - ecc * np.cos(eanom))

    # compute ra/dec offsets (size: n_orbs x n_dates)
    # math from James Graham. Lots of trig
    c2i2 = np.cos(0.5*inc)**2
    s2i2 = np.sin(0.5*inc)**2
    arg1 = tanom + argp + lan
    arg2 = tanom + argp - lan
    c1 = np.cos(arg1)
    c2 = np.cos(arg2)
    s1 = np.sin(arg1)
    s2 = np.sin(arg2)

    # updated sign convention for Green Eq. 19.4-19.7
    # return values in arcsecons
    plx_as = plx * 1e-3

    raoff = radius * (c2i2*s1 - s2i2*s2) * plx_as
    deoff = radius * (c2i2*c1 + s2i2*c2) * plx_as

    # compute the radial velocity (vz) of the body (size: n_orbs x n_dates)
    # first comptue the RV semi-amplitude (size: n_orbs)
    # Treat entries where mass = 0 (test particle) and massive bodies separately
    if mass == 0:
        # basically treating this body as a test particle. we can calcualte a radial velocity for a test particle
        Kv =  mean_motion * (sma * np.sin(inc)) / np.sqrt(1 - ecc**2) * (u.au/u.day)
        Kv = Kv.to(u.km/u.s) # converted to km/s
    else:
        # we want to measure the mass of the influencing body on the system
        # we need units now
        m2 = mtot - mass
        Kv = np.sqrt(consts.G / (1.0 - ecc**2)) * (m2 * u.Msun * np.sin(inc)) / np.sqrt(mtot * u.Msun) / np.sqrt(sma * u.au)
        Kv = Kv.to(u.km/u.s)
    # compute the vz
    vz =  Kv.value * ( ecc*np.cos(argp) + np.cos(argp + tanom) )

    # Squeeze out extra dimension (useful if n_orbs = 1, does nothing if n_orbs > 1)
    raoff = np.squeeze(raoff)
    deoff = np.squeeze(deoff)
    vz = np.squeeze(vz)

    return raoff, deoff, vz

def _calc_ecc_anom(manom, ecc, tolerance=1e-9, max_iter=100):
    """
    Computes the eccentric anomaly from the mean anomlay.
    Code from Rob De Rosa's orbit solver (e < 0.95 use Newton, e >= 0.95 use Mikkola)

    Args:
        manom (np.array): array of mean anomalies
        ecc (float): eccentricity
        tolerance (float, optional): absolute tolerance of iterative computation. Defaults to 1e-9.
        max_iter (int, optional): maximum number of iterations before switching. Defaults to 100.
    Return:
        eanom (np.array): array of eccentric anomalies

    Written: Jason Wang, 2018
    """

    eanom = np.full(np.shape(manom), np.nan)
    if np.isscalar(ecc): ecc = np.reshape(ecc, (1, ))

    # First deal with e == 0 elements
    ind_zero = np.where(ecc == 0.0)
    if len(ind_zero[0]) > 0: eanom[ind_zero] = manom[ind_zero]

    # Now low eccentricities
    ind_low = np.where(ecc < 0.95)
    if len(ind_low[0]) > 0: eanom[ind_low] = _newton_solver(manom[ind_low], ecc[ind_low], tolerance=tolerance, max_iter=max_iter)

    # Now high eccentricities
    ind_high = np.where(ecc >= 0.95)
    if len(ind_high[0]) > 0: eanom[ind_high] = _mikkola_solver_wrapper(manom[ind_high], ecc[ind_high])

    return eanom

def _newton_solver(manom, ecc, tolerance=1e-9, max_iter=100):
    """
    Newton-Raphson solver for eccentric anomaly.
    Args:
        manom (np.array): array of mean anomalies
        ecc (np.array): array of eccentricities
    Return:
        eanom (np.array): array of eccentric anomalies
    
    Written: Rob De Rosa, 2018

    """

    # Initialize at E = M. Probably could have a better choice of starting position
    eanom = np.copy(manom)

    # Let's do two iterations to start with
    eanom -= (eanom - (ecc * np.sin(eanom)) - manom) / (1.0 - (ecc * np.cos(eanom)))
    eanom -= (eanom - (ecc * np.sin(eanom)) - manom) / (1.0 - (ecc * np.cos(eanom)))

    diff = (eanom - (ecc * np.sin(eanom)) - manom) / (1.0 - (ecc * np.cos(eanom)))
    abs_diff = np.abs(diff)
    ind = np.where(abs_diff > tolerance)
    niter = 0
    while ((ind[0].size > 0) and (niter <= max_iter)):
        eanom[ind] -= diff[ind]
        diff[ind] = (eanom[ind] - (ecc[ind] * np.sin(eanom[ind])) - manom[ind]) / (1.0 - (ecc[ind] * np.cos(eanom[ind])))
        abs_diff[ind] = np.abs(diff[ind])
        ind = np.where(abs_diff > tolerance)
        niter += 1
    if niter >= max_iter:
        print(manom[ind], eanom[ind], ecc[ind], '> {} iter.'.format(max_iter))
        eanom[ind] = _mikkola_solver_wrapper(manom[ind], ecc[ind]) # Send remaining orbits to the analytical version, this has not happened yet...

    return eanom

def _mikkola_solver_wrapper(manom, ecc):
    """
    Analtyical Mikkola solver (S. Mikkola. 1987. Celestial Mechanics, 40 , 329-334.) for the eccentric anomaly.
    Wrapper for the python implemenation of the IDL version. From Rob De Rosa.

    Args:
        manom (np.array): array of mean anomalies
        ecc (float): eccentricity
    Return:
        eanom (np.array): array of eccentric anomalies

    Written: Jason Wang, 2018
    """
    ind_change = np.where(manom > np.pi)
    manom[ind_change] = (2.0 * np.pi) - manom[ind_change]
    eanom = _mikkola_solver(manom, ecc)
    eanom[ind_change] = (2.0 * np.pi) - eanom[ind_change]

    return eanom

def _mikkola_solver(manom, ecc):
    """
    Analtyical Mikkola solver for the eccentric anomaly.
    Adapted from IDL routine keplereq.pro by Rob De Rosa http://www.lpl.arizona.edu/~bjackson/idl_code/keplereq.pro

    Args:
        manom (np.array): array of mean anomalies
        ecc (float): eccentricity
    Return:
        eanom (np.array): array of eccentric anomalies

    Written: Jason Wang, 2018
    """

    alpha = (1.0 - ecc) / ((4.0 * ecc) + 0.5)
    beta = (0.5 * manom) / ((4.0 * ecc) + 0.5)

    aux = np.sqrt(beta**2.0 + alpha**3.0)
    z = beta + aux
    z = z**(1.0/3.0)

    s0 = z - (alpha/z)
    s1 = s0 - (0.078*(s0**5.0)) / (1.0 + ecc)
    e0 = manom + (ecc * (3.0*s1 - 4.0*(s1**3.0)))

    se0=np.sin(e0)
    ce0=np.cos(e0)

    f  = e0-ecc*se0-manom
    f1 = 1.0-ecc*ce0
    f2 = ecc*se0
    f3 = ecc*ce0
    f4 = -f2
    u1 = -f/f1
    u2 = -f/(f1+0.5*f2*u1)
    u3 = -f/(f1+0.5*f2*u2+(1.0/6.0)*f3*u2*u2)
    u4 = -f/(f1+0.5*f2*u3+(1.0/6.0)*f3*u3*u3+(1.0/24.0)*f4*(u3**3.0))

    return (e0 + u4)
