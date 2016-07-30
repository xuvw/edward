from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import multiprocessing
import numpy as np
import six
import tensorflow as tf

from edward.models import StanModel, Normal, PointMass
from edward.util import get_dims, get_session, hessian, kl_multivariate_normal, log_sum_exp, stop_gradient

try:
    import prettytensor as pt
except ImportError:
    pass


class Inference(object):
    """Base class for Edward inference methods.

    Attributes
    ----------
    latent_vars : list of str, or dict of str to RandomVariable
        Collection of random variables to perform inference on. If
        list, each random variable (of type `str`) will be inferred
        nonparametrically (e.g., MCMC). If dictionary, each random
        variable (of type `str`) is binded to to another random
        variable (of type `RandomVariable`); the latter will infer the
        former's posterior (e.g., VI).
    data : dict of tf.Tensor
        Data dictionary whose values may vary at each session run.
    model_wrapper : ed.Model
        Probability model.
    """
    def __init__(self, latent_vars, data=None, model_wrapper=None):
        """Initialization.

        Calls ``util.get_session()``

        Parameters
        ----------
        latent_vars : list of str, or dict of str to RandomVariable
            Collection of random variables to perform inference on. If
            list, each random variable (of type `str`) will be inferred
            nonparametrically (e.g., MCMC). If dictionary, each random
            variable (of type `str`) is binded to to another random
            variable (of type `RandomVariable`); the latter will infer the
            former's posterior (e.g., VI).
        data : dict, optional
            Data dictionary. For TensorFlow, Python, and Stan models,
            the key type is a string; for PyMC3, the key type is a
            Theano shared variable. For TensorFlow, Python, and PyMC3
            models, the value type is a NumPy array or TensorFlow
            tensor; for Stan, the value type is the type
            according to the Stan program's data block.
        model_wrapper : ed.Model
            Probability model.

        Notes
        -----
        If `data` is not passed in, the dictionary is empty.

        Three options are available for batch training:
        1. internally if user passes in data as a dictionary of NumPy
           arrays;
        2. externally if user passes in data as a dictionary of
           TensorFlow placeholders (and manually feeds them);
        3. externally if user passes in data as TensorFlow tensors
           which are the outputs of data readers.
        """
        sess = get_session()
        self.latent_vars = latent_vars
        if model_wrapper is None:
            raise NotImplementedError()

        self.model_wrapper = model_wrapper

        if data is None:
            data = {}

        if isinstance(model_wrapper, StanModel):
            # Stan models do no support data subsampling because they
            # take arbitrary data structure types in the data block
            # and not just NumPy arrays (this makes it unamenable to
            # TensorFlow placeholders). Therefore fix the data
            # dictionary `self.data` at compile time to `data`.
            self.data = data
        else:
            self.data = {}
            for key, value in six.iteritems(data):
                if isinstance(value, tf.Tensor):
                    # If `data` has TensorFlow placeholders, the user
                    # must manually feed them at each step of
                    # inference.
                    # If `data` has tensors that are the output of
                    # data readers, then batch training operates
                    # according to the reader.
                    self.data[key] = value
                elif isinstance(value, np.ndarray):
                    # If `data` has NumPy arrays, store the data
                    # in the computational graph.
                    placeholder = tf.placeholder(tf.float32, value.shape)
                    var = tf.Variable(placeholder, trainable=False, collections=[])
                    self.data[key] = var
                    sess.run(var.initializer, {placeholder: value})
                else:
                    raise NotImplementedError()


class MonteCarlo(Inference):
    """Base class for Monte Carlo inference methods.
    """
    def __init__(self, *args, **kwargs):
        """Initialization.

        Parameters
        ----------
        latent_vars : list of str
            Collection of random variables to perform inference on.
            Each random variable (of type `str`) will be inferred
            nonparametrically (e.g., MCMC).
        data : dict, optional
            Data dictionary. For TensorFlow, Python, and Stan models,
            the key type is a string; for PyMC3, the key type is a
            Theano shared variable. For TensorFlow, Python, and PyMC3
            models, the value type is a NumPy array or TensorFlow
            placeholder; for Stan, the value type is the type
            according to the Stan program's data block.
        model_wrapper : ed.Model
            Probability model.
        """
        super(MonteCarlo, self).__init__(*args, **kwargs)


class VariationalInference(Inference):
    """Base class for variational inference methods.
    """
    def __init__(self, latent_vars, data=None, model_wrapper=None):
        """Initialization.

        Parameters
        ----------
        latent_vars : dict of str to RandomVariable
            Collection of random variables to perform inference on.
            Each random variable (of type `str`) is binded to to
            another random variable (of type `RandomVariable`); the
            latter will infer the former's posterior.
        data : dict, optional
            Data dictionary. For TensorFlow, Python, and Stan models,
            the key type is a string; for PyMC3, the key type is a
            Theano shared variable. For TensorFlow, Python, and PyMC3
            models, the value type is a NumPy array or TensorFlow
            placeholder; for Stan, the value type is the type
            according to the Stan program's data block.
        model_wrapper : ed.Model
            Probability model.
        """
        super(VariationalInference, self).__init__(latent_vars, data, model_wrapper)

    def run(self, *args, **kwargs):
        """A simple wrapper to run variational inference.

        1. Initialize via ``initialize``.
        2. Run ``update`` for ``self.n_iter`` iterations.
        3. While running, ``print_progress``.
        4. Finalize via ``finalize``.

        Parameters
        ----------
        *args
            Passed into ``initialize``.
        **kwargs
            Passed into ``initialize``.
        """
        self.initialize(*args, **kwargs)
        for t in range(self.n_iter+1):
            loss = self.update()
            self.print_progress(t, loss)

        self.finalize()

    def initialize(self, n_iter=1000, n_minibatch=None, n_print=100,
        optimizer=None, scope=None):
        """Initialize variational inference algorithm.

        Set up ``tf.train.AdamOptimizer`` with a decaying scale factor.

        Initialize all variables

        Parameters
        ----------
        n_iter : int, optional
            Number of iterations for optimization.
        n_minibatch : int, optional
            Number of samples for data subsampling. Default is to use
            all the data. Subsampling is available only if all data
            passed in are NumPy arrays and the model is not a Stan
            model. For subsampling details, see
            `tf.train.slice_input_producer` and `tf.train.batch`.
        n_print : int, optional
            Number of iterations for each print progress. To suppress print
            progress, then specify None.
        optimizer : str, optional
            Whether to use TensorFlow optimizer or PrettyTensor
            optimizer when using PrettyTensor. Defaults to TensorFlow.
        scope : str, optional
            Scope of TensorFlow variable objects to optimize over.
        """
        self.n_iter = n_iter
        self.n_minibatch = n_minibatch
        self.n_print = n_print
        self.loss = tf.constant(0.0)

        if n_minibatch is not None and not isinstance(self.model_wrapper, StanModel):
            # Re-assign data to batch tensors, with size given by `n_data`.
            values = list(six.itervalues(self.data))
            slices = tf.train.slice_input_producer(values)
            # By default use as many threads as CPUs.
            batches = tf.train.batch(slices, n_minibatch,
                                     num_threads=multiprocessing.cpu_count())
            if not isinstance(batches, list):
                # `tf.train.batch` returns tf.Tensor if `slices` is a
                # list of size 1.
                batches = [batches]

            self.data = {key: value for key, value in
                         zip(six.iterkeys(self.data), batches)}

        loss = self.build_loss()
        if optimizer is None:
            var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                         scope=scope)
            # Use ADAM with a decaying scale factor.
            global_step = tf.Variable(0, trainable=False)
            starter_learning_rate = 0.1
            learning_rate = tf.train.exponential_decay(starter_learning_rate,
                                                global_step,
                                                100, 0.9, staircase=True)
            optimizer = tf.train.AdamOptimizer(learning_rate)
            self.train = optimizer.minimize(loss, global_step=global_step,
                                            var_list=var_list)
        else:
            if scope is not None:
                raise NotImplementedError("PrettyTensor optimizer does not accept a variable scope.")

            optimizer = tf.train.AdamOptimizer(0.01, epsilon=1.0)
            self.train = pt.apply_optimizer(optimizer, losses=[loss])

        init = tf.initialize_all_variables()
        init.run()

        # Start input enqueue threads.
        self.coord = tf.train.Coordinator()
        self.threads = tf.train.start_queue_runners(coord=self.coord)

    def update(self):
        """Run one iteration of optimizer for variational inference.

        Returns
        -------
        loss : double
            Loss function values after one iteration
        """
        sess = get_session()
        _, loss = sess.run([self.train, self.loss])
        return loss

    def print_progress(self, t, loss):
        """Print progress to output.

        Parameters
        ----------
        t : int
            Iteration counter
        loss : double
            Loss function value at iteration ``t``
        """
        if self.n_print is not None:
            if t % self.n_print == 0:
                print("iter {:d} loss {:.2f}".format(t, loss))
                for rv in six.itervalues(self.latent_vars):
                    print(rv)

    def finalize(self):
        """Function to call after convergence.

        Any class based on ``VariationalInference`` **may**
        overwrite this method.
        """
        # Ask threads to stop.
        self.coord.request_stop()
        self.coord.join(self.threads)

    def build_loss(self):
        """Build loss function.

        Empty method.

        Any class based on ``VariationalInference`` **must**
        implement this method.

        Raises
        ------
        NotImplementedError
        """
        raise NotImplementedError()


class MFVI(VariationalInference):
    """Mean-field variational inference.

    This class implements a variety of "black-box" variational inference
    techniques (Ranganath et al., 2014) that minimize

    .. math::

        KL( q(z; \lambda) || p(z | x) ).

    This is equivalent to maximizing the objective function (Jordan et al., 1999)

    .. math::

        ELBO =  E_{q(z; \lambda)} [ \log p(x, z) - \log q(z; \lambda) ].
    """
    def __init__(self, *args, **kwargs):
        super(MFVI, self).__init__(*args, **kwargs)

    def initialize(self, n_samples=1, score=None, *args, **kwargs):
        """Initialization.

        Parameters
        ----------
        n_samples : int, optional
            Number of samples from variational model for calculating
            stochastic gradients.
        score : bool, optional
            Whether to force inference to use the score function
            gradient estimator. Otherwise default is to use the
            reparameterization gradient if available.
        """
        if score is None and \
           all([rv.is_reparameterized and rv.is_differentiable
                for rv in six.itervalues(self.latent_vars)]):
            self.score = False
        else:
            self.score = True

        self.n_samples = n_samples
        return super(MFVI, self).initialize(*args, **kwargs)

    def build_loss(self):
        """Wrapper for the MFVI loss function.

        .. math::

            -ELBO =  -E_{q(z; \lambda)} [ \log p(x, z) - \log q(z; \lambda) ]

        MFVI supports

        1. score function gradients
        2. reparameterization gradients

        of the loss function.

        If the variational model is a Gaussian distribution, then part of the
        loss function can be computed analytically.

        If the variational model is a normal distribution and the prior is
        standard normal, then part of the loss function can be computed
        analytically following Kingma and Welling (2014),

        .. math::

            E[\log p(x | z) + KL],

        where the KL term is computed analytically.

        Returns
        -------
        result :
            an appropriately selected loss function form
        """
        q_is_normal = all([isinstance(rv, Normal) for
                           rv in six.itervalues(self.latent_vars)])
        if self.score:
            if q_is_normal and hasattr(self.model_wrapper, 'log_lik'):
                return self.build_score_loss_kl()
            # Analytic entropies may lead to problems around
            # convergence; for now it is deactivated.
            #elif is_entropy:
            #    return self.build_score_loss_entropy()
            else:
                return self.build_score_loss()
        else:
            if q_is_normal and hasattr(self.model_wrapper, 'log_lik'):
                return self.build_reparam_loss_kl()
            #elif is_entropy:
            #    return self.build_reparam_loss_entropy()
            else:
                return self.build_reparam_loss()

    def build_score_loss(self):
        """Build loss function. Its automatic differentiation
        is a stochastic gradient of

        .. math::

            -ELBO =  -E_{q(z; \lambda)} [ \log p(x, z) - \log q(z; \lambda) ]

        based on the score function estimator. (Paisley et al., 2012)

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the
        expectation using Monte Carlo sampling.
        """
        z = {rv: rv.sample(self.n_samples) for rv in six.itervalues(self.latent_vars)}
        # Collect dictionary binding each random variable in the
        # probability model to its realization.
        xz = self.data
        for key, value in six.iteritems(self.latent_vars):
            xz[key] = z[value]

        p_log_prob = self.model_wrapper.log_prob(xz)

        q_log_prob = 0.0
        for rv, obs in six.iteritems(z):
            q_log_prob += rv.log_prob(stop_gradient(obs))

        losses = p_log_prob - q_log_prob
        self.loss = tf.reduce_mean(losses)
        return -tf.reduce_mean(q_log_prob * stop_gradient(losses))

    def build_reparam_loss(self):
        """Build loss function. Its automatic differentiation
        is a stochastic gradient of

        .. math::

            -ELBO =  -E_{q(z; \lambda)} [ \log p(x, z) - \log q(z; \lambda) ]

        based on the reparameterization trick. (Kingma and Welling, 2014)

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the
        expectation using Monte Carlo sampling.
        """
        z = {rv: rv.sample(self.n_samples) for rv in six.itervalues(self.latent_vars)}
        # Collect dictionary binding each random variable in the
        # probability model to its realization.
        xz = self.data
        for key, value in six.iteritems(self.latent_vars):
            xz[key] = z[value]

        p_log_prob = self.model_wrapper.log_prob(xz)

        q_log_prob = 0.0
        for rv, obs in six.iteritems(z):
            q_log_prob += rv.log_prob(obs)

        self.loss = tf.reduce_mean(p_log_prob - q_log_prob)
        return -self.loss

    def build_score_loss_kl(self):
        """Build loss function. Its automatic differentiation
        is a stochastic gradient of

        .. math::

            -ELBO =  - ( E_{q(z; \lambda)} [ \log p(x | z) ]
                         + KL(q(z; \lambda) || p(z)) )

        based on the score function estimator. (Paisley et al., 2012)

        It assumes the KL is analytic.

        It assumes the prior is :math:`p(z) = \mathcal{N}(z; 0, 1)`.

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the
        expectation using Monte Carlo sampling.
        """
        z = {rv: rv.sample(self.n_samples) for rv in six.itervalues(self.latent_vars)}
        # Collect dictionary binding each random variable in the
        # probability model to its realization.
        xz = self.data
        for key, value in six.iteritems(self.latent_vars):
            xz[key] = z[value]

        p_log_lik = self.model_wrapper.log_lik(xz)

        q_log_prob = 0.0
        for rv, obs in six.iteritems(z):
            q_log_prob += rv.log_prob(stop_gradient(obs))

        mu = tf.pack([rv.loc for rv in six.itervalues(self.latent_vars)])
        sigma = tf.pack([rv.scale for rv in six.itervalues(self.latent_vars)])
        kl = kl_multivariate_normal(mu, sigma)
        self.loss = tf.reduce_mean(p_log_lik) - kl
        return -(tf.reduce_mean(q_log_prob * stop_gradient(p_log_lik)) - kl)

    def build_score_loss_entropy(self):
        """Build loss function. Its automatic differentiation
        is a stochastic gradient of

        .. math::

            -ELBO =  - ( E_{q(z; \lambda)} [ \log p(x, z) ]
                        + H(q(z; \lambda)) )

        based on the score function estimator. (Paisley et al., 2012)

        It assumes the entropy is analytic.

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the
        expectation using Monte Carlo sampling.
        """
        z = {rv: rv.sample(self.n_samples) for rv in six.itervalues(self.latent_vars)}
        # Collect dictionary binding each random variable in the
        # probability model to its realization.
        xz = self.data
        for key, value in six.iteritems(self.latent_vars):
            xz[key] = z[value]

        p_log_prob = self.model_wrapper.log_prob(xz)

        q_log_prob = 0.0
        q_entropy = 0.0
        for rv, obs in six.iteritems(z):
            q_log_prob += rv.log_prob(stop_gradient(obs))
            q_entropy += rv.entropy()

        self.loss = tf.reduce_mean(p_log_prob) + q_entropy
        return -(tf.reduce_mean(q_log_prob * stop_gradient(p_log_prob)) +
                 q_entropy)

    def build_reparam_loss_kl(self):
        """Build loss function. Its automatic differentiation
        is a stochastic gradient of

        .. math::

            -ELBO =  - ( E_{q(z; \lambda)} [ \log p(x | z) ]
                        + KL(q(z; \lambda) || p(z)) )

        based on the reparameterization trick. (Kingma and Welling, 2014)

        It assumes the KL is analytic.

        It assumes the prior is :math:`p(z) = \mathcal{N}(z; 0, 1)`

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the
        expectation using Monte Carlo sampling.
        """
        z = {rv: rv.sample(self.n_samples) for rv in six.itervalues(self.latent_vars)}
        # Collect dictionary binding each random variable in the
        # probability model to its realization.
        xz = self.data
        for key, value in six.iteritems(self.latent_vars):
            xz[key] = z[value]

        p_log_lik = self.model_wrapper.log_lik(xz)

        mu = tf.pack([rv.loc for rv in six.itervalues(self.latent_vars)])
        sigma = tf.pack([rv.scale for rv in six.itervalues(self.latent_vars)])
        self.loss = tf.reduce_mean(p_log_lik) - \
                    kl_multivariate_normal(mu, sigma)
        return -self.loss

    def build_reparam_loss_entropy(self):
        """Build loss function. Its automatic differentiation
        is a stochastic gradient of

        .. math::

            -ELBO =  -( E_{q(z; \lambda)} [ \log p(x , z) ]
                        + H(q(z; \lambda)) )

        based on the reparameterization trick. (Kingma and Welling, 2014)

        It assumes the entropy is analytic.

        Computed by sampling from :math:`q(z;\lambda)` and evaluating the
        expectation using Monte Carlo sampling.
        """
        z = {rv: rv.sample(self.n_samples) for rv in six.itervalues(self.latent_vars)}
        # Collect dictionary binding each random variable in the
        # probability model to its realization.
        xz = self.data
        for key, value in six.iteritems(self.latent_vars):
            xz[key] = z[value]

        self.model_wrapper.log_prob(xz)

        q_entropy = 0.0
        for rv in six.iterkeys(z):
            q_entropy += rv.entropy()

        self.loss = tf.reduce_mean(p_log_prob) + q_entropy
        return -self.loss


class KLpq(VariationalInference):
    """A variational inference method that minimizes the Kullback-Leibler
    divergence from the posterior to the variational model (Cappe et al., 2008)

    .. math::

        KL( p(z |x) || q(z) ).
    """
    def __init__(self, *args, **kwargs):
        super(KLpq, self).__init__(*args, **kwargs)

    def initialize(self, n_samples=1, *args, **kwargs):
        """Initialization.

        Parameters
        ----------
        n_samples : int, optional
            Number of samples from variational model for calculating
            stochastic gradients.
        """
        self.n_samples = n_samples
        return super(KLpq, self).initialize(*args, **kwargs)

    def build_loss(self):
        """Build loss function. Its automatic differentiation
        is a stochastic gradient of

        .. math::
            KL( p(z |x) || q(z) )
            =
            E_{p(z | x)} [ \log p(z | x) - \log q(z; \lambda) ]

        based on importance sampling.

        Computed as

        .. math::
            1/B \sum_{b=1}^B [ w_{norm}(z^b; \lambda) *
                                (\log p(x, z^b) - \log q(z^b; \lambda) ]

        where

        .. math::
            z^b \sim q(z^b; \lambda)

            w_{norm}(z^b; \lambda) = w(z^b; \lambda) / \sum_{b=1}^B ( w(z^b; \lambda) )

            w(z^b; \lambda) = p(x, z^b) / q(z^b; \lambda)

        which gives a gradient

        .. math::
            - 1/B \sum_{b=1}^B
            w_{norm}(z^b; \lambda) \partial_{\lambda} \log q(z^b; \lambda)

        """
        z = {rv: rv.sample(self.n_samples) for rv in six.itervalues(self.latent_vars)}
        # Collect dictionary binding each random variable in the
        # probability model to its realization.
        xz = self.data
        for key, value in six.iteritems(self.latent_vars):
            xz[key] = z[value]

        # normalized importance weights
        q_log_prob = 0.0
        for rv, obs in six.iteritems(z):
            q_log_prob += rv.log_prob(stop_gradient(obs))

        log_w = self.model_wrapper.log_prob(xz) - q_log_prob
        log_w_norm = log_w - log_sum_exp(log_w)
        w_norm = tf.exp(log_w_norm)

        self.loss = tf.reduce_mean(w_norm * log_w)
        return -tf.reduce_mean(q_log_prob * stop_gradient(w_norm))


class MAP(VariationalInference):
    """Maximum a posteriori inference.

    We implement this using a ``PointMass`` variational distribution to
    solve the following optimization problem

    .. math::

        \min_{z} - \log p(x,z)
    """
    # TODO
    def __init__(self, latent_vars, data=None, model_wrapper=None):
        if isinstance(latent_vars, list):
            with tf.variable_scope("variational"):
                if hasattr(model_wrapper, 'n_vars'):
                    latent_vars = {'z': PointMass(model_wrapper.n_vars)}
                else:
                    latent_vars = {'z': PointMass(0)}
        elif isinstance(latent_vars, dict):

        else:
            raise TypeError()

        super(MAP, self).__init__(latent_vars, data, model_wrapper)

    def build_loss(self):
        """Build loss function. Its automatic differentiation
        is the gradient of

        .. math::
            - \log p(x,z)
        """
        z = {rv: rv.sample() for rv in six.itervalues(self.latent_vars)}
        # Collect dictionary binding each random variable in the
        # probability model to its realization.
        xz = self.data
        for key, value in six.iteritems(self.latent_vars):
            xz[key] = z[value]

        self.loss = tf.squeeze(self.model_wrapper.log_prob(xz))
        return -self.loss


class Laplace(MAP):
    """Laplace approximation.

    It approximates the posterior distribution using a normal
    distribution centered at the mode of the posterior.

    We implement this by running ``MAP`` to find the posterior mode.
    This forms the mean of the normal approximation. We then compute
    the Hessian at the mode of the posterior. This forms the
    covariance of the normal approximation.
    """
    # TODO
    def __init__(self, latent_vars, data=None, model_wrapper=None):
        super(Laplace, self).__init__(data, model_wrapper, params)

    def finalize(self):
        """Function to call after convergence.

        Computes the Hessian at the mode.
        """
        # use only a batch of data to estimate hessian
        x = self.data
        z = {rv: rv.sample() for rv in six.itervalues(self.latent_vars)}
        xz = self.data
        for key, value in six.iteritems(self.latent_vars):
            xz[key] = z[value]

        var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES,
                                     scope='variational')
        inv_cov = hessian(self.model_wrapper.log_prob(xz), var_list)
        print("Precision matrix:")
        print(inv_cov.eval())
        super(Laplace, self).finalize()
