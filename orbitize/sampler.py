from __future__ import print_function
import numpy as np
import astropy.units as u
import astropy.constants as consts
import sys
import abc

import emcee
import ptemcee

import orbitize.lnlike
import orbitize.priors
import orbitize.kepler
from orbitize.system import radec2seppa
import orbitize.results

import matplotlib.pyplot as plt

# Python 2 & 3 handle ABCs differently
if sys.version_info[0] < 3:
    ABC = abc.ABCMeta('ABC', (), {})
else:
    ABC = abc.ABC

class Sampler(ABC):
    """
    Abstract base class for sampler objects.
    All sampler objects should inherit from this class.

    Written: Sarah Blunt, 2018
    """

    def __init__(self, system, like='chi2_lnlike', custom_lnlike=None):
        self.system = system

        # check if `like` is a string or a function
        if callable(like):
            self.lnlike = like
        else:
            self.lnlike = getattr(orbitize.lnlike, like)

        self.custom_lnlike = custom_lnlike

    @abc.abstractmethod
    def run_sampler(self, total_orbits):
        pass

    def _logl(self, params):
        """
        log likelihood function that interfaces with the orbitize objects
        Comptues the sum of the log likelihoods of the data given the input model

        Args:
            params (np.array of float): RxM array
                of fitting parameters, where R is the number of
                parameters being fit, and M is the number of orbits
                we need model predictions for. Must be in the same order
                documented in System() above. If M=1, this can be a 1d array.

        Returns:
            lnlikes (float): sum of all log likelihoods of the data given input model

        """
        # compute the model based on system params
        model = self.system.compute_model(params)

        # fold data/errors to match model output shape. In particualr, quant1/quant2 are interleaved
        data = np.array([self.system.data_table['quant1'], self.system.data_table['quant2']]).T
        errs = np.array([self.system.data_table['quant1_err'], self.system.data_table['quant2_err']]).T

        # TODO: THIS ONLY WORKS FOR 1 PLANET. Make this a for loop to work for multiple planets.
        seppa_indices = np.union1d(self.system.seppa[0], self.system.seppa[1])

        # compute lnlike
        lnlikes =  self.lnlike(data, errs, model, seppa_indices)

        # return sum of lnlikes (aka product of likeliehoods)
        lnlikes_sum = np.nansum(lnlikes, axis=(0,1))

        if self.custom_lnlike is not None:
            lnlikes_sum += self.custom_lnlike(params)

        return lnlikes_sum


class OFTI(Sampler):
    """
    OFTI Sampler

    Args:
        like (string): name of likelihood function in ``lnlike.py``
        system (system.System): ``system.System`` object
        custom_lnlike (func): ability to include an addition custom likelihood function in the fit.
            the function looks like ``clnlikes = custon_lnlike(params)`` where ``params is a RxM array
            of fitting parameters, where R is the number of orbital paramters (can be passed in system.compute_model()),
            and M is the number of orbits we need model predictions for. It returns ``clnlikes`` which is an array of
            length M, or it can be a single float if M = 1.

    Written: Isabel Angelo, Sarah Blunt, Logan Pearce, 2018
    """
    def __init__(self, system, like='chi2_lnlike', custom_lnlike=None):

        super(OFTI, self).__init__(system, like=like, custom_lnlike=custom_lnlike)

        # compute priors and columns containing ra/dec and sep/pa
        self.priors = self.system.sys_priors

        # convert RA/Dec rows to sep/PA
        body_num = 1 # the first planet; MODIFY THIS LATER FOR MULTIPLE PLANETS
        if len(self.system.radec[body_num]) > 0:
            print('Converting ra/dec data points in data_table to sep/pa. Original data are stored in input_table.')
            self.system.convert_data_table_radec2seppa(body_num=body_num)

        # these are of type astropy.table.column
        self.sep_observed = self.system.data_table[:]['quant1'].copy()
        self.pa_observed = self.system.data_table[:]['quant2'].copy()
        self.sep_err = self.system.data_table[:]['quant1_err'].copy()
        self.pa_err = self.system.data_table[:]['quant2_err'].copy()

        ### this is OK, ONLY IF we are only using self.epochs for computing RA/Dec from Keplerian elements
        self.epochs = np.array(self.system.data_table['epoch']) - self.system.tau_ref_epoch

        # choose scale-and-rotate epoch
        self.epoch_idx = np.argmin(self.sep_err) # epoch with smallest error

        # create an empty results object
        self.results = orbitize.results.Results(
            sampler_name = self.__class__.__name__,
            post = None,
            lnlike = None,
            tau_ref_epoch=self.system.tau_ref_epoch
        )

    def prepare_samples(self, num_samples):
        """
        Prepare some orbits for rejection sampling. This draws random orbits
        from priors, and performs scale & rotate.

        Args:
            num_samples (int): number of orbits to draw and scale & rotate for
                OFTI to run rejection sampling on

        Return:
            np.array: array of prepared samples. The first dimension has size of
            num_samples. This should be passed into ``OFTI.reject()``
        """

        # TODO: modify to work for multi-planet systems

        # generate sample orbits
        samples = np.empty([len(self.priors), num_samples])
        for i in range(len(self.priors)):
            if hasattr(self.priors[i], "draw_samples"):
                samples[i, :] = self.priors[i].draw_samples(num_samples)
            else: # param is fixed & has no prior
                samples[i, :] = self.priors[i] * np.ones(num_samples)

        sma, ecc, inc, argp, lan, tau, plx, mtot = [s for s in samples]

        period_prescale = np.sqrt(
            4*np.pi**2*(sma*u.AU)**3/(consts.G*(mtot*u.Msun))
        )
        period_prescale = period_prescale.to(u.day).value
        meananno = self.epochs[self.epoch_idx]/period_prescale - tau

        # compute sep/PA of generated orbits
        ra, dec, vc = orbitize.kepler.calc_orbit(
            self.epochs[self.epoch_idx], sma, ecc, inc, argp, lan, tau, plx, mtot
        )
        sep, pa = orbitize.system.radec2seppa(ra, dec) # sep[mas], PA[deg]

        # generate Gaussian offsets from observational uncertainties
        sep_offset = np.random.normal(
            0, self.sep_err[self.epoch_idx], size=num_samples
        )
        pa_offset =  np.random.normal(
            0, self.pa_err[self.epoch_idx], size=num_samples
        )

        # calculate correction factors
        sma_corr = (sep_offset + self.sep_observed[self.epoch_idx])/sep
        lan_corr = (pa_offset + self.pa_observed[self.epoch_idx] - pa)

        # perform scale-and-rotate
        sma *= sma_corr # [AU]
        lan += np.radians(lan_corr) # [rad]
        lan = lan % (2*np.pi)

        period_new = np.sqrt(
            4*np.pi**2*(sma*u.AU)**3/(consts.G*(mtot*u.Msun))
        )
        period_new = period_new.to(u.day).value

        tau = (self.epochs[self.epoch_idx]/period_new - meananno) % 1

        # updates samples with new values of sma, pan, tau
        samples[0,:] = sma
        samples[4,:] = lan
        samples[5,:] = tau

        return samples


    def reject(self, samples):
        """
        Runs rejection sampling on some prepared samples.

        Args:
            samples (np.array): array of prepared samples. The first dimension \
                has size ``num_samples``. This should be the output of \
                ``prepare_samples()``.

        Return:
            tuple:

                np.array: a subset of ``samples`` that are accepted based on the
                data.

                np.array: the log likelihood values of the accepted orbits.

        """
        lnp = self._logl(samples)

        # reject orbits with probability less than a uniform random number
        random_samples = np.log(np.random.random(len(lnp)))
        saved_orbit_idx = np.where(lnp > random_samples)[0]
        saved_orbits = np.array([samples[:,i] for i in saved_orbit_idx])
        lnlikes = np.array([lnp[i] for i in saved_orbit_idx])

        return saved_orbits, lnlikes


    def run_sampler(self, total_orbits, num_samples=10000):
        """
        Runs OFTI until we get the number of total accepted orbits we want.

        Args:
            total_orbits (int): total number of accepted orbits desired by user
            num_samples (int): number of orbits to prepare for OFTI to run
                rejection sampling on

        Return:
            output_orbits (np.array): array of accepted orbits. First dimension
            has size ``total_orbits``.
        """

        n_orbits_saved = 0
        output_orbits = np.empty((total_orbits, len(self.priors)))
        output_lnlikes = np.empty(total_orbits)

        # add orbits to `output_orbits` until `total_orbits` are saved
        while n_orbits_saved < total_orbits:
            samples = self.prepare_samples(num_samples)
            accepted_orbits, lnlikes = self.reject(samples)

            if len(accepted_orbits)==0:
                pass
            else:
                n_accepted = len(accepted_orbits)
                maxindex2save = np.min([n_accepted, total_orbits - n_orbits_saved])

                output_orbits[n_orbits_saved : n_orbits_saved+n_accepted] = accepted_orbits[0:maxindex2save]
                output_lnlikes[n_orbits_saved : n_orbits_saved+n_accepted] = lnlikes[0:maxindex2save]
                n_orbits_saved += maxindex2save

                # print progress statement
                print(str(n_orbits_saved)+'/'+str(total_orbits)+' orbits found',end='\r')

        self.results.add_samples(
            np.array(output_orbits),
            output_lnlikes
        )

        return np.array(output_orbits)


class MCMC(Sampler):
    """
    MCMC sampler. Supports either parallel tempering or just regular MCMC. Parallel tempering will be run if ``num_temps`` > 1
    Parallel-Tempered MCMC Sampler uses ptemcee, a fork of the emcee Affine-infariant sampler
    Affine-Invariant Ensemble MCMC Sampler uses emcee.

    .. Warning:: may not work well for multi-modal distributions

    Args:
        system (system.System): system.System object
        num_temps (int): number of temperatures to run the sampler at. Parallel tempering will be
            used if num_temps > 1 (default=20)
        num_walkers (int): number of walkers at each temperature (default=1000)
        num_threads (int): number of threads to use for parallelization (default=1)
        like (str): name of likelihood function in ``lnlike.py``
        custom_lnlike (func): ability to include an addition custom likelihood function in the fit.
            the function looks like ``clnlikes = custon_lnlike(params)`` where ``params is a RxM array
            of fitting parameters, where R is the number of orbital paramters (can be passed in system.compute_model()),
            and M is the number of orbits we need model predictions for. It returns ``clnlikes`` which is an array of
            length M, or it can be a single float if M = 1.

    Written: Jason Wang, Henry Ngo, 2018
    """
    def __init__(self, system, num_temps=20, num_walkers=1000, num_threads=1, like='chi2_lnlike', custom_lnlike=None):

        super(MCMC, self).__init__(system, like=like, custom_lnlike=custom_lnlike)

        self.num_temps = num_temps
        self.num_walkers = num_walkers
        self.num_threads = num_threads

        # create an empty results object
        self.results = orbitize.results.Results(
            sampler_name = self.__class__.__name__,
            post = None,
            lnlike = None,
            tau_ref_epoch=system.tau_ref_epoch
        )

        if self.num_temps > 1:
            self.use_pt = True
        else:
            self.use_pt = False
            self.num_temps = 1

        # get priors from the system class. need to remove and record fixed priors
        self.priors = []
        self.fixed_params = []
        for i, prior in enumerate(system.sys_priors):

            # check for fixed parameters
            if not hasattr(prior, "draw_samples"):
                self.fixed_params.append((i, prior))
            else:
                self.priors.append(prior)

        # initialize walkers initial postions
        self.num_params = len(self.priors)
        init_positions = []
        for prior in self.priors:
            # draw them uniformly becase we don't know any better right now
            # TODO: be smarter in the future
            random_init = prior.draw_samples(num_walkers*self.num_temps)
            if self.num_temps > 1:
                random_init = random_init.reshape([self.num_temps, num_walkers])

            init_positions.append(random_init)

        # save this as the current position for the walkers
        if self.use_pt:
            # make this an numpy array, but combine the parameters into a shape of (ntemps, nwalkers, nparams)
            # we currently have a list of [ntemps, nwalkers] with nparam arrays. We need to make nparams the third dimension
            self.curr_pos = np.dstack(init_positions)
        else:
            # make this an numpy array, but combine the parameters into a shape of (nwalkers, nparams)
            # we currently have a list of arrays where each entry is num_walkers prior draws for each parameter
            # We need to make nparams the second dimension, so we have to transpose the stacked array
            self.curr_pos = np.stack(init_positions).T



    def _fill_in_fixed_params(self, sampled_params):
        """
        Fills in the missing parameters from the chain that aren't being sampeld

        Args:
            sampled_params (np.array): either 1-D array of size = number of sampled params, or 2-D array of shape (num_models, num_params)

        Returns:
            full_params (np.array): same number of dimensions as sampled_params, but with num_params including the fixed parameters
        """
        if len(self.fixed_params) == 0:
            # nothing to add
            return sampled_params

        # check if 1-D or 2-D
        twodim = np.ndim(sampled_params) == 2

        # insert in params
        for index, value in self.fixed_params:
            if twodim:
                sampled_params = np.insert(sampled_params, index, value, axis=1)
            else:
                sampled_params = np.insert(sampled_params, index, value)

        return sampled_params

    def _logl(self, params, include_logp=False):
        """
        log likelihood function that interfaces with the orbitize objects
        Comptues the sum of the log likelihoods of the data given the input model

        Args:
            params (np.array of float): MxR array
                of fitting parameters, where R is the number of
                parameters being fit, and M is the number of orbits
                we need model predictions for. Must be in the same order
                documented in System() above. If M=1, this can be a 1d array.

            include_logp (bool): if True, also include log prior in this function

        Returns:
            lnlikes (float): sum of all log likelihoods of the data given input model

        """
        if include_logp:
            if np.ndim(params) == 1:
                logp = orbitize.priors.all_lnpriors(params, self.priors)
            else:
                logp = np.array([orbitize.priors.all_lnpriors(pset, self.priors) for pset in params])
        else:
            logp = 0 # don't include prior

        full_params = self._fill_in_fixed_params(params)
        if np.ndim(full_params) == 2:
            full_params = full_params.T

        return super(MCMC, self)._logl(full_params) + logp

    def run_sampler(self, total_orbits, burn_steps=0, thin=1):
        """
        Runs PT MCMC sampler. Results are stored in ``self.chain`` and ``self.lnlikes``.
        Results also added to ``orbitize.results.Results`` object (``self.results``)

        .. Note:: Can be run multiple times if you want to pause and inspect things.
            Each call will continue from the end state of the last execution.

        Args:
            total_orbits (int): total number of accepted possible
                orbits that are desired. This equals
                ``num_steps_per_walker`` x ``num_walkers``
            burn_steps (int): optional paramter to tell sampler
                to discard certain number of steps at the beginning
            thin (int): factor to thin the steps of each walker
                by to remove correlations in the walker steps

        Returns:
            ``emcee.sampler`` object: the sampler used to run the MCMC
        """

        if self.use_pt:
            sampler = ptemcee.Sampler(
                self.num_walkers, self.num_params, self._logl, orbitize.priors.all_lnpriors,
                ntemps=self.num_temps, threads=self.num_threads, logpargs=[self.priors,]
            )
        else:
            sampler = emcee.EnsembleSampler(
                self.num_walkers, self.num_params, self._logl,
                threads=self.num_threads, kwargs={'include_logp' : True}
            )

        for pos, lnprob, lnlike in sampler.sample(self.curr_pos, iterations=burn_steps, thin=thin):
            pass

        sampler.reset()
        try:
            self.curr_pos = pos
        except UnboundLocalError: # 0 step burn-in (pos is not defined)
            pass
        print('Burn in complete')

        nsteps = int(np.ceil(total_orbits / self.num_walkers))

        assert (nsteps > 0), 'Total_orbits must be greater than num_walkers.'

        i=0
        for pos, lnprob, lnlike in sampler.sample(p0=self.curr_pos, iterations=nsteps, thin=thin):
            i+=1
            # print progress statement
            if i%5==0:
                print(str(i)+'/'+str(nsteps)+' steps completed',end='\r')
        print('')

        self.curr_pos = pos

        # TODO: Need something here to pick out temperatures, just using lowest one for now
        self.chain = sampler.chain

        if self.use_pt:
            self.post = sampler.flatchain[0,:,:]
            self.lnlikes = sampler.logprobability[0,:,:].flatten() # should also be picking out the lowest temperature logps
            self.lnlikes_alltemps = sampler.logprobability
        else:
            self.post = sampler.flatchain
            self.lnlikes = sampler.lnprobability

        # include fixed parameters in posterior
        self.post = self._fill_in_fixed_params(self.post)

        self.results.add_samples(self.post,self.lnlikes)

        print('Run complete')

        return sampler

    def examine_chains(self, param_list=None, temp=0, walker_list=None, n_walkers=None, step_range=None):
        """
        Plots position of walkers at each step. Returns list of figures, one per parameter
        Args:
            param_list: List of strings of parameters to plot (e.g. "sma1")
                If None (default), all parameters are plotted
            temp (int): The temperature to plot (defaults to 0, the lowest temp)
            walker_list: List or array of walker numbers to plot
                If None (default), all walkers are plotted
            n_walkers (int): Randomly select `n_walkers` to plot
                Overrides walker_list if this is set
                If None (default), walkers selected as per `walker_list`
            step_range (array or tuple): Start and end values of step numbers to plot
                If None (default), all the steps are plotted

        Returns:
            List of ``matplotlib.pyplot.Figure`` objects: 
                Walker position plot for each parameter selected

        (written): Henry Ngo, 2019
        """
        
        # Check that valid temperature is given
        if temp < 0 or temp >= self.num_temps:
            raise Exception('Invalid value provided for temp: {}. Must be integer in [0,num_temps)'.format(temp))

        # Get the correct temperature
        if self.num_temps > 1:
            try:
                chn = self.chain[temp,:,:] # Entire MCMC chain for given temperature
            except:
                raise Exception(
                    'Invalid value provided for temp: {}. Must be integer in [0,num_temps)'.format(temp))
        else: # Not Parallel Tempering, so self.chain has no temperature info
            chn = self.chain # The entire MCMC chain
    
        # Get list of walkers to use 
        if n_walkers is not None: # If n_walkers defined, randomly choose that many walkers
            walkers_to_plot = np.random.choice(self.num_walkers,size=n_walkers,replace=False)
        elif walker_list is not None: # if walker_list is given, use that list
            walkers_to_plot = np.array(walker_list)
        else: # both n_walkers and walker_list are none, so use all walkers
            walkers_to_plot = np.arange(self.num_walkers)
        
        # Get list of parameters to use
        if param_list is None:
            params_to_plot = np.arange(self.num_params)
        else: # build list from user input strings
            params_plot_list = []
            for i in param_list:
                if i in self.system.param_idx:
                    params_plot_list.append(self.system.param_idx[i])
                else:
                    raise Exception('Invalid param name: {}. See system.param_idx.'.format(i))
            params_to_plot = np.array(params_plot_list)

        # Loop through each parameter and make plot
        output_figs = []
        for pp in params_to_plot:
            fig, ax = plt.subplots()
            for ww in walkers_to_plot:
                ax.plot(chn[ww,:,pp],'k-')
            ax.set_xlabel('Step')
            if step_range is not None: # Limit range shown if step_range is set
                ax.set_xlim(step_range)
            output_figs.append(fig)
        
        # Return
        return output_figs

    def chop_chains(self, burn, trim=0):
        """
        Permanently removes steps from beginning (and/or end) of chains by updating Results object.
        Args:
            burn (int): The number of steps to remove from the beginning of the chains
            trim (int): The number of steps to remove from the end of the chians (optional)

        Returns:
            None. Updates the bookkeeping array in both `Sampler` and associated `Results` objects

        (written): Henry Ngo, 2019
        """
        
        # Find beginning and end indices
        n_steps = self.chain.shape[-2] # chain has shape (ntemps, nwalkers, nsteps, nparams) or (nwalkers, nsteps, nparams)
        keep_start = burn
        keep_end = n_steps - trim

        # Make copies of arrays to be modified
        new_chain = np.copy(self.chain)
        new_lnlikes = np.copy(self.lnlikes)
        if self.use_pt:
            new_lnlikes_alltemps = np.copy(self.lnlikes_alltemps)
        new_post = np.copy(self.post)

        # Need to "unflatten" the flattened arrays in order to index by step
        new_lnlikes = new_lnlikes.reshape((self.num_walkers,n_steps))
        if self.use_pt:
            new_lnlikes_alltemps = new_lnlikes_alltemps.reshape((self.num_temps,self.num_walkers,n_steps))
        new_post = new_post.reshape((self.num_walkers, n_steps, self.num_params))
    
        # Update arrays in `sampler`: chain, lnlikes, lnlikes_alltemps (if PT), post
        if self.use_pt:
            chop_chain = np.copy(new_chain[:, :, keep_start:keep_end, :])
            chop_lnlikes_alltemps = np.copy(new_lnlikes_alltemps[:, :, keep_start:keep_end])
        else:
            chop_chain = np.copy(new_chain[:, keep_start:keep_end, :])
        chop_lnlikes = np.copy(new_lnlikes[:, keep_start:keep_end])
        chop_post = np.copy(self.post[:, keep_start:keep_end,:]))

        # Flatten likelihoods and samples
        self.chain = chop_chain
        self.lnlikes = chop_lnlikes.reshape(self.num_walkers*nsteps)
        if self.use_pt:
            self.lnlikes_alltemps = chop_lnlikes_alltemps.reshape(self.num_temps,self.num_walkers*nsteps)
        self.post = chop_post.reshape(self.num_walkers*nsteps,self.num_params)

        # Update results object