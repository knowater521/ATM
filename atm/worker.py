#!/usr/bin/python2.7
from atm.config import *
from atm.constants import *
from atm.utilities import *
from atm.model import Model
from atm.database import Database, ClassifierStatus
from btb.tuning.constants import Tuners

import argparse
import ast
import datetime
import imp
import os
import pdb
import random
import socket
import sys
import time
import traceback
import warnings
from collections import defaultdict
from decimal import Decimal
from operator import attrgetter

import numpy as np
import pandas as pd
from boto.s3.connection import S3Connection, Key as S3Key

# shhh
warnings.filterwarnings('ignore')

# for garrays
os.environ['GNUMPY_IMPLICIT_CONVERSION'] = 'allow'

# get the file system in order
# make sure we have directories where we need them
MODELS_DIR = 'models'
METRICS_DIR = 'metrics'
LOG_DIR = 'logs'
ensure_directory(MODELS_DIR)
ensure_directory(METRICS_DIR)
ensure_directory(LOG_DIR)

# name log file after the local hostname
LOG_FILE = os.path.join(LOG_DIR, '%s.txt' % socket.gethostname())

# how long to sleep between loops while waiting for new dataruns to be added
LOOP_WAIT = 0


# TODO: use python's logging module instead of this
def _log(msg, stdout=True):
    with open(LOG_FILE, 'a') as lf:
        lf.write(msg + '\n')
    if stdout:
        print msg


# Exception thrown when something goes wrong for the worker, but the worker
# handles the error.
class ClassifierError(Exception):
    pass


class Worker(object):
    def __init__(self, database, datarun, save_files=False, cloud_mode=False,
                 aws_config=None):
        """
        database: Database object with connection information
        datarun: Datarun ORM object to work on.
        save_files: if True, save model and metrics files to disk or cloud.
        cloud_mode: if True, save classifiers to the cloud
        aws_config: S3Config object with amazon s3 connection info
        """
        self.db = database
        self.datarun = datarun
        self.save_files = save_files
        self.cloud_mode = cloud_mode
        self.aws_config = aws_config

        # load the Dataset from the database
        self.dataset = self.db.get_dataset(self.datarun.dataset_id)

        # load the Selector and Tuner classes specified by our datarun
        self.load_selector()
        self.load_tuner()

    def load_selector(self):
        """
        Load and initialize the BTB class which will be responsible for
        selecting hyperpartitions.
        """
        # selector will either be a key into SELECTORS_MAP or a path to
        # a file that defines a class called CustomSelector.
        if self.datarun.selector in SELECTORS_MAP:
            Selector = SELECTORS_MAP[self.datarun.selector]
        else:
            mod = imp.load_source('btb.selection.custom', self.datarun.selector)
            Selector = mod.CustomSelector
        _log('Selector: %s' % Selector)

        # generate the arguments we need to initialize the selector
        hyperpartitions = self.db.get_hyperpartitions(datarun_id=self.datarun.id)
        hp_by_method = defaultdict(list)
        for hp in hyperpartitions:
            hp_by_method[hp.method].append(hp.id)
        hyperpartition_ids = [hp.id for hp in hyperpartitions]

        # Selector classes support passing in redundant arguments
        self.selector = Selector(choices=hyperpartition_ids,
                                 k=self.datarun.k_window,
                                 by_algorithm=dict(hp_by_method))

    def load_tuner(self):
        """
        Load, but don't initialize, the BTB class which will be responsible for
        choosing non-hyperpartition hyperparameter values (a subclass of Tuner). The
        tuner must be initialized with information about the hyperpartition, so it
        cannot be created until later.
        """
        # tuner will either be a key into TUNERS_MAP or a path to
        # a file that defines a class called CustomTuner.
        if self.datarun.tuner in TUNERS_MAP:
            self.Tuner = TUNERS_MAP[self.datarun.tuner]
        else:
            mod = imp.load_source('btb.tuning.custom', self.datarun.tuner)
            self.Tuner = mod.CustomTuner
        _log('Tuner: %s' % self.Tuner)

    def load_data(self):
        """
        Download a set of train/test data from AWS (if necessary) and then load
        it from disk into memory.

        Returns: train/test data in the structures consumed by
            wrapper.load_data_from_objects(), i.e. (trainX, testX, trainY,
            testY)
        """
        # TODO TODO
        # if the data are not present locally, check the S3 bucket detailed in
        # the config for it.
        if not os.path.isfile(self.dataset.train_path):
            ensure_directory(dw.outfolder)
            if download_file_s3(dw.train_path_out, aws_key=self.aws_config.access_key,
                                aws_secret=self.aws_config.access_key,
                                s3_bucket=self.aws_config.s3_bucket,
                                s3_folder=self.aws_config.s3_folder) != dw.train_path_out:
                raise Exception('Something about train dataset caching is wrong...')

        # load the data into matrix format
        trainX = read_atm_csv(dw.train_path_out)
        trainY = trainX[:, self.dataset.label_column]
        trainX = np.delete(trainX, self.dataset.label_column, axis=1)

        if not os.path.isfile(dw.test_path_out):
            ensure_directory(dw.outfolder)
            if download_file_s3(dw.test_path_out, aws_key=self.aws_key,
                                aws_secret=self.aws_secret,
                                s3_bucket=self.s3_bucket,
                                s3_folder=self.s3_folder) != dw.test_path_out:
                raise Exception('Something about test dataset caching is wrong...')

        # load the data into matrix format
        testX = read_atm_csv(dw.test_path_out)
        testY = testX[:, self.dataset.label_column]
        testX = np.delete(testX, self.dataset.label_column, axis=1)

        return trainX, testX, trainY, testY

    def save_classifier_cloud(self, local_model_path, local_metric_path):
        """
        Save a classifier to the S3 bucket supplied by aws_config. Saves a
        serialized representaion of the model as well as a detailed set
        of metrics.

        local_model_path: path to serialized model in the local file system
        local_metric_path: path to serialized metrics in the local file system
        """
        conn = S3Connection(aws_key, aws_secret)
        bucket = conn.get_bucket(s3_bucket)

        if aws_folder:
            aws_model_path = os.path.join(aws_folder, local_model_path)
            aws_metric_path = os.path.join(aws_folder, local_metric_path)
        else:
            aws_model_path = local_model_path
            aws_metric_path = local_metric_path

        kmodel = S3Key(bucket)
        kmodel.key = aws_model_path
        kmodel.set_contents_from_filename(local_model_path)
        _log('Uploading model at %s to S3 bucket %s' % (s3_bucket,
                                                        local_model_path))

        kmodel = S3Key(bucket)
        kmodel.key = aws_metric_path
        kmodel.set_contents_from_filename(local_metric_path)
        _log('Uploading metrics at %s to S3 bucket %s' % (s3_bucket,
                                                          local_metric_path))

        # delete the local copy of the model & metrics so that they don't fill
        # up the worker instance's hard drive
        _log('Deleting local copies of %s and %s' % (local_model_path,
                                                     local_metric_path))
        os.remove(local_model_path)
        os.remove(local_metric_path)

    def save_classifier(self, classifier_id, model, performance):
        """
        Update a classifier with performance and model information and mark it as
        "complete"

        classifier_id: ID of the classifier to save
        model: Model object containing a serializable representation of the
            final model generated by this classifier
        performance: dictionary containing detailed performance data, as
            generated by the Wrapper object that actually tests the classifier.
        """
        classifier = self.db.get_classifier(classifier_id)
        phash = hash_dict(classifier.params)
        rhash = hash_string(self.dataset.name)

        # whether to save model and performance data to the filesystem
        if self.save_files:
            local_model_path = make_model_path(MODELS_DIR, phash, rhash,
                                               self.datarun.description)
            _log('Saving model in: %s' % local_model_path)
            joblib.dump(model, local_model_path, compress=9)

            local_metric_path = make_metric_path(METRICS_DIR, phash, rhash,
                                                 self.datarun.description)
            metric_obj = dict(cv=performance['cv_object'],
                              test=performance['test_object'])
            _log('Saving metrics in: %s' % local_model_path)
            save_metric(local_metric_path, object=metric_obj)

            # if necessary, save model and metrics to Amazon S3 bucket
            if self.cloud_mode:
                try:
                    self.save_classifier_cloud(local_model_path, local_metric_path)
                except Exception:
                    msg = traceback.format_exc()
                    _log('Error in save_classifier_cloud()')
                    self.db.mark_classifier_errored(classifier_id, error_msg=msg)
        else:
            local_model_path = None
            local_metric_path = None

        # update the classifier in the database
        self.db.complete_classifier(classifier_id=classifier_id,
                                    trainable_params=model.algorithm.trainable_params,
                                    dimensions=model.algorithm.dimensions,
                                    model_path=local_model_path,
                                    metric_path=local_metric_path,
                                    cv_score=performance['cv_judgment_metric'],
                                    cv_stdev=performance['cv_judgment_metric_stdev'],
                                    test_score=performance['test_judgment_metric'])

        # update this session's hyperpartition entry
        _log('Saved classifier %d.' % classifier_id)

    def select_hyperpartition(self):
        """
        Use the hyperpartition selection method specified by our datarun to choose a
        hyperpartition of hyperparameters from the ModelHub. Only consider
        partitions for which gridding is not complete.
        """
        hyperpartitions = self.db.get_hyperpartitions(datarun_id=self.datarun.id)

        # load classifiers and build scores lists
        # make sure all hyperpartitions are present in the dict, even ones that
        # don't have any classifiers. That way the selector can choose hyperpartitions
        # that haven't been scored yet.
        hyperpartition_scores = {fs.id: [] for fs in hyperpartitions}
        classifiers = self.db.get_classifiers(datarun_id=self.datarun.id)
                                              #status=ClassifierStatus.COMPLETE)
        for c in classifiers:
            # ignore hyperpartitions for which gridding is done
            if c.hyperpartition_id not in hyperpartition_scores:
                continue

            # the cast to float is necessary because the score is a Decimal;
            # doing Decimal-float arithmetic throws errors later on.
            score = float(getattr(c, self.datarun.score_target) or 0)
            hyperpartition_scores[c.hyperpartition_id].append(score)

        hyperpartition_id = self.selector.select(hyperpartition_scores)
        return self.db.get_hyperpartition(hyperpartition_id)

    def tune_parameters(self, hyperpartition):
        """
        Use the hyperparameter tuning method specified by our datarun to choose
        a set of hyperparameters from the potential space.
        """
        # Get parameter metadata for this hyperpartition
        tunables = hyperpartition.tunables

        # If there aren't any tunable parameters, we're done. Return the vector
        # of values in the hyperpartition and mark the set as finished.
        if not len(tunables):
            _log('No tunables for hyperpartition %d' % hyperpartition.id)
            self.db.mark_hyperpartition_gridding_done(hyperpartition.id)
            return vector_to_params(vector=[],
                                    tunables=tunables,
                                    categoricals=hyperpartition.categoricals,
                                    constants=hyperpartition.constants)

        # Get previously-used parameters: every classifier should either be
        # completed or have thrown an error
        classifiers = [l for l in self.db.get_classifiers(hyperpartition_id=hyperpartition.id)
                       if l.status == ClassifierStatus.COMPLETE]

        # Extract parameters and scores as numpy arrays from classifiers
        X = params_to_vectors([l.params for l in classifiers], tunables)
        y = np.array([float(getattr(l, self.datarun.score_target))
                      for l in classifiers])

        # Initialize the tuner and propose a new set of parameters
        # this has to be initialized with information from the hyperpartition, so we
        # need to do it fresh for each classifier (not in load_tuner)
        tuner = self.Tuner(tunables=tunables,
                           gridding=self.datarun.gridding,
                           r_min=self.datarun.r_min)
        tuner.fit(X, y)
        vector = tuner.propose()

        if vector is None and self.datarun.gridding:
            _log('Gridding done for hyperpartition %d' % hyperpartition.id)
            self.db.mark_hyperpartition_gridding_done(hyperpartition.id)
            return None

        # Convert the numpy array of parameters to a form that can be
        # interpreted by ATM, then return.
        return vector_to_params(vector=vector,
                                tunables=tunables,
                                categoricals=hyperpartition.categoricals,
                                constants=hyperpartition.constants)

    def is_datarun_finished(self):
        """
        Check to see whether the datarun is finished. This could be due to the
        budget being exhausted or due to hyperparameter gridding being done.
        """
        hyperpartitions = self.db.get_hyperpartitions(datarun_id=self.datarun.id)
        if not hyperpartitions:
            _log('No incomplete hyperpartitions for datarun %d present in database.'
                 % self.datarun.id)
            return True

        if self.datarun.budget_type == 'classifier':
            # hyperpartition classifier counts are updated whenever a classifier
            # is created, so this will count running, errored, and complete.
            n_completed = sum([p.classifiers for p in hyperpartitions])
            if n_completed >= self.datarun.budget:
                _log('Classifier budget has run out!')
                return True

        elif self.datarun.budget_type == 'walltime':
            deadline = self.datarun.deadline
            if datetime.datetime.now() > deadline:
                _log('Walltime budget has run out!')
                return True

        return False

    def test_classifier(self, classifier, params):
        """
        Given a set of fully-qualified hyperparameters, create and test a
        classification model.
        Returns: Model object and performance dictionary
        """
        model = Model(classifier.code, params, self.datarun.metric)
        performance = model.train_test(self.dataset.train_path,
                                       self.dataset.test_path)

        old_best = self.db.get_best_classifier(datarun_id=self.datarun.id)
        if old_best is not None:
            old_val = old_best.cv_judgment_metric
            old_err = 2 * old_best.cv_judgment_metric_stdev

        new_val = performance['cv_judgment_metric']
        new_err = 2 * performance['cv_judgment_metric_stdev']

        _log('Judgment metric (%s): %.3f +- %.3f' %
             (self.datarun.metric, new_val, new_err))

        if old_best is not None:
            if (new_val - new_err) > ():
                _log('New best score! Previous best (classifier %s): %.3f +- %.3f' %
                     (old_best.id, old_val, old_err))
            else:
                _log('Best so far (classifier %s): %.3f +- %.3f' %
                     (old_best.id, old_val, old_err))

        return wrapper, performance

    def run_classifier(self):
        """
        Choose hyperparameters, then use them to test and save a Classifier.
        """
        # check to see if our work is done
        if self.is_datarun_finished():
            # marked the run as done successfully
            self.db.mark_datarun_complete(self.datarun.id)
            _log('Datarun %d has ended.' % self.datarun.id)
            return

        try:
            _log('Choosing hyperparameters...')
            # use the multi-arm bandit to choose which hyperpartition to use next
            hyperpartition = self.select_hyperpartition()
            # use our tuner to choose a set of parameters for the hyperpartition
            params = self.tune_parameters(hyperpartition)
        except Exception as e:
            _log('Error choosing hyperparameters: datarun=%s' % str(self.datarun))
            _log(traceback.format_exc())
            raise ClassifierError()

        if params is None:
            _log('No parameters chosen: hyperpartition %d is finished.' %
                 hyperpartition.id)
            return

        _log('Chose parameters for method %s:' % hyperpartition.method)
        for k, v in params.items():
            _log('\t%s = %s' % (k, v))

        # TODO: this doesn't belong here
        params['function'] = hyperpartition.method

        _log('Creating classifier...')
        classifier = self.db.create_classifier(hyperpartition_id=hyperpartition.id,
                                               datarun_id=self.datarun.id,
                                               host=get_public_ip(),
                                               params=params)

        try:
            _log('Testing classifier...')
            model, performance = self.test_classifier(classifier.id, params)
            _log('Saving classifier...')
            self.save_classifier(classifier.id, model, performance)
        except Exception as e:
            msg = traceback.format_exc()
            _log('Error testing classifier: datarun=%s' % str(self.datarun))
            _log(msg)
            self.db.mark_classifier_errored(classifier.id, error_msg=msg)
            raise ClassifierError()


def work(db, datarun_ids=None, save_files=False, choose_randomly=True,
         cloud_mode=False, aws_config=None, total_time=None, wait=True):
    """
    Check the ModelHub database for unfinished dataruns, and spawn workers to
    work on them as they are added. This process will continue to run until it
    exceeds total_time or is broken with ctrl-C.

    db: Database instance with which we can make queries to ModelHub
    datarun_ids (optional): list of IDs of dataruns to compute on. If None,
        this will work on all unfinished dataruns in the database.
    choose_randomly: if True, work on all highest-priority dataruns in random
        order. If False, work on them in sequential order (by ID)
    cloud_mode: if True, save processed datasets to AWS. If this option is set,
        aws_config must be supplied.
    aws_config (optional): if cloud_mode is set, this myst be an AWSConfig
        object with connection details for an S3 bucket.
    total_time (optional): if set to an integer, this worker will only work for
        total_time seconds. Otherwise, it will continue working until all
        dataruns are complete (or indefinitely).
    wait: if True, once all dataruns in the database are complete, keep spinning
        and wait for new runs to be added. If False, exit once all dataruns are
        complete.
    """
    start_time = datetime.datetime.now()

    # main loop
    while True:
        # get all pending and running dataruns, or all pending/running dataruns
        # from the list we were given
        dataruns = db.get_dataruns(include_ids=datarun_ids)
        if not dataruns:
            if wait:
                _log('No dataruns found. Sleeping %d seconds and trying again.' %
                     LOOP_WAIT)
                time.sleep(LOOP_WAIT)
                continue
            else:
                break

        max_priority = max([r.priority for r in dataruns])
        priority_runs = [r for r in dataruns if r.priority == max_priority]

        # either choose a run randomly, or take the run with the lowest ID
        if choose_randomly:
            run = random.choice(priority_runs)
        else:
            run = sorted(dataruns, key=attrgetter('id'))[0]

        # say we've started working on this datarun, if we haven't already
        db.mark_datarun_running(run.id)

        _log('=' * 25)
        _log('Computing on datarun %d' % run.id)
        # actual work happens here
        worker = Worker(db, run, save_files=save_files,
                        cloud_mode=cloud_mode, aws_config=aws_config)
        try:
            worker.run_classifier()
        except ClassifierError as e:
            # the exception has already been handled; just wait a sec so we
            # don't go out of control reporting errors
            _log('Something went wrong. Sleeping %d seconds.' % LOOP_WAIT)
            time.sleep(LOOP_WAIT)

        elapsed_time = (datetime.datetime.now() - start_time).total_seconds()
        if total_time is not None and elapsed_time >= total_time:
            _log('Total run time for worker exceeded; exiting.')
            break


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Add more classifiers to database')
    add_arguments_sql(parser)
    add_arguments_aws_s3(parser)

    # add worker-specific arguments
    parser.add_argument('--cloud-mode', action='store_true', default=False,
                        help='Whether to run this worker in cloud mode')
    parser.add_argument('--dataruns', help='Only train on dataruns with these ids',
                        nargs='+')
    parser.add_argument('--time', help='Number of seconds to run worker', type=int)
    parser.add_argument('--choose-randomly', action='store_true',
                        help='Choose dataruns to work on randomly (default = sequential order)')
    parser.add_argument('--no-save', dest='save_files', default=True,
                        action='store_const', const=False,
                        help="don't save models and metrics for later")

    # parse arguments and load configuration
    args = parser.parse_args()
    sql_config, _, aws_config = load_config(args=args)

    # let's go
    work(db=Database(**vars(sql_config)),
         datarun_ids=args.dataruns,
         choose_randomly=args.choose_randomly,
         save_files=args.save_files,
         cloud_mode=args.cloud_mode,
         aws_config=aws_config,
         total_time=args.time)
