import os
from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn import metrics
from sklearn import model_selection
from sklearn.pipeline import Pipeline, FeatureUnion
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.linear_model import LinearRegression, LogisticRegressionCV
from sklearn.metrics import roc_curve, auc
from sklearn.model_selection import RandomizedSearchCV
from sklearn.neighbors import KNeighborsClassifier

from healthcareai.common import helpers
from healthcareai.common import model_eval
from healthcareai.common.healthcareai_error import HealthcareAIError
from healthcareai.common.helpers import count_unique_elements_in_column
from healthcareai.common.transformers import DataFrameImputer, DataFrameConvertTargetToBinary, \
    DataFrameCreateDummyVariables
from healthcareai.common.filters import DataframeDateTimeColumnSuffixFilter, DataframeGrainColumnDataFilter, \
    DataframeNullValueFilter

from imblearn.under_sampling import RandomUnderSampler
from imblearn.over_sampling import RandomOverSampler
from sklearn.preprocessing import StandardScaler

import json


class DevelopSupervisedModel(object):
    """
    This class helps create a model using several common classifiers
    (reporting AUC) and regressors (reporting MAE/MSE). When instantiating,
    the data is prepped and one-fifth is held out so model accuracy can be
    assessed.

    Parameters
    ----------
    modeltype (str) : whether the model will be 'classification' or 'regression'
    df (dataframe) : data that your model is based on
    predictedcol (str) : y column (in ticks) who's values are being predicted
    impute (boolean) : whether imputation is done on the data; if not, rows with nulls are removed
    graincol (str) : OPTIONAL | column (in ticks) that represents the data's grain
    debug (boolean) : OPTIONAL | verbosity of the output

    Returns
    -------
    Object representing the cleaned data, against which methods are run
    """

    def __init__(self, dataframe, model_type, predicted_column, grain_column_name=None, verbose=False):
        self.dataframe = dataframe
        self.model_type = model_type
        self.predicted_column = predicted_column
        self.grain_column_name = grain_column_name
        self.verbose = verbose
        self.X_train = None
        self.X_test = None
        self.y_train = None
        self.y_test = None
        # TODO implement (or avoid) these attributes, which really might be methods
        self.y_probab_linear = None
        self.y_probab_rf = None
        self.col_list = None
        self.rfclf = None
        self.au_roc = None
        self.results = None

        self.console_log(
            'Shape and top 5 rows of original dataframe:\n{}\n{}'.format(self.dataframe.shape, self.dataframe.head()))

    def data_preparation_pipeline(self, impute=True):
        """Main data preparation pipeline. Sequentially runs transformers and methods to clean and prepare the data"""
        column_removal_pipeline = Pipeline([
            ('dts_filter', DataframeDateTimeColumnSuffixFilter()),
            ('grain_column_filter', DataframeGrainColumnDataFilter(self.grain_column_name)),
        ])

        transformation_pipeline = Pipeline([
            ('null_row_filter', DataframeNullValueFilter(excluded_columns=None)),
            ('convert_target_to_binary', DataFrameConvertTargetToBinary(self.model_type, self.predicted_column)),
            ('dummify', DataFrameCreateDummyVariables(self.predicted_column)),
        ])

        self.dataframe = column_removal_pipeline.fit_transform(self.dataframe)

        # Perform one of two basic imputation methods
        # TODO we need to think about making this optional to solve the problem of rare and very predictive values
        #   where neither imputation or dropping rows is appropriate
        if impute is True:
            self.dataframe = DataFrameImputer().fit_transform(self.dataframe)

        self.dataframe = transformation_pipeline.fit_transform(self.dataframe)

        return self.dataframe

    def under_sampling(self, random_state=0):
        # TODO convert to fit transform
        # NB: Must be done BEFORE train/test split
        #     so that when we split the under/over sampled
        #     dataset. We do under/over sampling on
        #     the entire dataframe.
        #     Must be done after imputation, since
        #     under/over sampling will not work with
        #     missing values.
        #     Must be done after target column is converted to
        #     numerical value (so under/over sampling from
        #     imblearn works).
        y = np.squeeze(self.dataframe[[self.predicted_column]])
        X = self.dataframe.drop([self.predicted_column], axis=1)

        under_sampler = RandomUnderSampler(random_state=random_state)
        X_under_sampled, y_under_sampled = under_sampler.fit_sample(X, y)

        X_under_sampled = pd.DataFrame(X_under_sampled)
        X_under_sampled.columns = X.columns
        y_under_sampled = pd.Series(y_under_sampled)

        dataframe_under_sampled = X_under_sampled
        dataframe_under_sampled[self.predicted_column] = y_under_sampled
        self.dataframe = dataframe_under_sampled

    def over_sampling(self, random_state=0):
        # TODO convert to fit transform
        # NB: Must be done BEFORE train/test split
        #     so that when we split the under/over sampled
        #     dataset. We do under/over sampling on
        #     the entire dataframe.
        #     Must be done after imputation, since
        #     under/over sampling will not work with
        #     missing values.
        #     Must be done after target column is converted to
        #     numerical value (so under/over sampling from
        #     imblearn works).
        y = np.squeeze(self.dataframe[[self.predicted_column]])
        X = self.dataframe.drop([self.predicted_column], axis=1)

        over_sampler = RandomOverSampler(random_state=random_state)
        X_over_sampled, y_over_sampled = over_sampler.fit_sample(X, y)

        X_over_sampled = pd.DataFrame(X_over_sampled)
        X_over_sampled.columns = X.columns
        y_over_sampled = pd.Series(y_over_sampled)

        dataframe_over_sampled = X_over_sampled
        dataframe_over_sampled[self.predicted_column] = y_over_sampled
        self.dataframe = dataframe_over_sampled

    def feature_scaling(self, columns_to_scale):
        # TODO convert to fit transform
        # NB: Must happen AFTER self.X_train, self.X_test,
        #     self.y_train, self.y_test are defined.
        #     Must happen AFTER imputation is done so there
        #     are no missing values.
        #     Must happen AFTER under/over sampling is done
        #     so that we scale the under/over sampled dataset.
        # TODO: How to warn the user if they call this method at the wrong time?
        X_train_scaled_subset = self.X_train[columns_to_scale]
        X_test_scaled_subset = self.X_test[columns_to_scale]
        scaler = StandardScaler()

        scaler.fit(X_train_scaled_subset)

        X_train_scaled_subset_dataframe = pd.DataFrame(scaler.transform(X_train_scaled_subset))
        X_train_scaled_subset_dataframe.index = X_train_scaled_subset.index
        X_train_scaled_subset_dataframe.columns = X_train_scaled_subset.columns
        self.X_train[columns_to_scale] = X_train_scaled_subset_dataframe

        X_test_scaled_subset_dataframe = pd.DataFrame(scaler.transform(X_test_scaled_subset))
        X_test_scaled_subset_dataframe.index = X_test_scaled_subset.index
        X_test_scaled_subset_dataframe.columns = X_test_scaled_subset.columns
        self.X_test[columns_to_scale] = X_test_scaled_subset_dataframe

    def print_out_dataframe_shape_and_head(self, message):
        self.console_log(message)
        self.console_log(self.dataframe.shape)
        self.console_log(self.dataframe.head())

    def train_test_split(self):
        y = np.squeeze(self.dataframe[[self.predicted_column]])
        X = self.dataframe.drop([self.predicted_column], axis=1)

        self.X_train, self.X_test, self.y_train, self.y_test = model_selection.train_test_split(
            X, y, test_size=.20, random_state=0)

        self.console_log('\nShape of X_train: {}\ny_train: {}\nX_test: {}\ny_test: {}'.format(
            self.X_train.shape,
            self.y_train.shape,
            self.X_test.shape,
            self.y_test.shape))

    def save_output_to_csv(self, filename, output):
        # TODO timeRan is borked
        output_dataframe = pd.DataFrame([(timeRan, self.model_type, output['modelLabels'],
                                          output['gridSearch_BestScore'],
                                          output['gridSearch_ScoreMetric'],) \
                                         + x for x in list(output.items())], \
                                        columns=['TimeStamp', 'ModelType',
                                                 'ModelLabels', 'BestScore',
                                                 'BestScoreMetric', 'Metric',
                                                 'MetricValue']).set_index('TimeStamp')
        # save files locally #
        output_dataframe.to_csv(filename + '.txt', header=False)

    def ensemble_regression(self, scoring_metric='roc_auc', model_by_name=None):
        # TODO stub
        pass

    def ensemble_classification(self, scoring_metric='roc_auc', model_by_name=None):
        """
        This provides a simple way to put data in and have healthcare.ai train a few models and pick the best one for
        your data.
        """
        # TODO enumerate, document and validate scoring options
        # http://scikit-learn.org/stable/modules/model_evaluation.html#common-cases-predefined-values
        # TODO Does one of those options make the most sense to pick a default?
        # TODO Can we algorithmically determine the best choice?

        self.validate_score_metric_for_number_of_classes(scoring_metric)
        score_by_name = {}

        # Here is the default list of algorithms to try for the ensemble
        # Adding an ensemble method is as easy as adding a new key:value pair in the `model_by_name` dictionary
        if model_by_name is None:
            model_by_name = {}
            model_by_name['KNN'] = self.knn(randomized_search=True, scoring_metric=scoring_metric)
            model_by_name['Logistic Regression'] = self.logistic_regression()
            model_by_name['Random Forest Classifier'] = self.random_forest_classifier(
                randomized_search=True,
                scoring_metric=scoring_metric).best_estimator_

        for name, model in model_by_name.items():
            # TODO this may need to ferret out each classification score separately
            score = self.calculate_classification_metric(model)
            score_by_name[name] = score[scoring_metric]

            self.console_log('{} algorithm: score = {}'.format(name, score))

        sorted_names_and_scores = sorted(score_by_name.items(), key=lambda x: x[1])
        best_algorithm_name, best_score = sorted_names_and_scores[-1]
        best_model = model_by_name[best_algorithm_name]

        results = {
            'best_score': best_score,
            'best_algorithm_name': best_algorithm_name,
            'model_scores': score_by_name,
            'best_model': best_model
        }

        print('Based on the scoring metric {}, the best algorithm found is: {}'.format(scoring_metric,
                                                                                       best_algorithm_name))
        print('{} {} = {}'.format(best_algorithm_name, scoring_metric, best_score))

        self.results = results
        return results

    def write_classification_metrics_to_json(self):
        # TODO a similar method should be created for regression metrics
        output = {}
        y_pred = self.results['best_model'].predict(self.X_test)
        accuracy = metrics.accuracy_score(self.y_test, y_pred)
        confusion_matrix = metrics.confusion_matrix(self.y_test, y_pred)
        output['accuracy'] = accuracy
        output['confusion_matrix'] = confusion_matrix.tolist()
        output['auc_roc'] = self.results['best_score']
        output['algorithm_name'] = self.results['best_algorithm_name']
        with open('classification_metrics.json', 'w') as fp:
            json.dump(output, fp, indent=4, sort_keys=True)

    def validate_score_metric_for_number_of_classes(self, metric):
        # TODO make this more robust for other scoring metrics
        """
        Check that a user's choice of scoring metric makes sense with the number of prediction classes
        :param metric: a string of the scoring metric
        """
        classes = count_unique_elements_in_column(self.dataframe, self.predicted_column)
        if classes is 2:
            pass
        elif classes > 2 and metric is 'roc_auc':
            raise (HealthcareAIError(
                'AUC (aka roc_auc) cannot be used for more than two classes. Please choose another metric such as \'accuracy\''))

    def calculate_classification_metric(self, trained_model, scoring_metric='roc_auc'):
        """
        Given a trained model, calculate the selected metric
        :param trained_model: a scikit-learn estimator that has been `.fit()`
        :param scoring_metric: The scoring metric to optimized for if using random search.
            See http://scikit-learn.org/stable/modules/model_evaluation.html
        :return: the metric
        """
        predictions = trained_model.predict(self.X_test)
        result = {'roc_auc_score': metrics.roc_auc_score(self.y_test, predictions),
                  'accuracy': metrics.accuracy_score(self.y_test, predictions)}

        return result

    def calculate_regression_metric(self, trained_model):
        """
        Given a trained model, calculate the selected metric
        :param trained_model: a scikit-learn estimator that has been `.fit()`
        :return: an object with two common regression metrics
        """
        predictions = trained_model.predict(self.X_test)
        mean_squared_error = metrics.mean_squared_error(self.y_test, predictions)
        mean_absolute_error = metrics.mean_absolute_error(self.y_test, predictions)

        result = {'mean_squared_error': mean_squared_error, 'mean_absolute_error': mean_absolute_error}

        return result

    def logistic_regression(self, scoring_metric='roc_auc', hyperparameter_grid=None, randomized_search=True):
        """
        A light wrapper for Sklearn's logistic regression that performs randomized search over a default (and
        overideable) hyperparameter grid.
        """
        if hyperparameter_grid is None:
            # TODO sensible default hyperparameter grid
            pass
            # hyperparameter_grid = {'n_neighbors': neighbor_list, 'weights': ['uniform', 'distance']}

        algorithm = prepare_randomized_search(
            LogisticRegressionCV,
            scoring_metric,
            hyperparameter_grid,
            randomized_search,
            # 5 cross validation folds
            cv=5)

        algorithm.fit(self.X_train, self.y_train)

        return algorithm

    def linear_regression(self, scoring_metric='roc_auc', hyperparameter_grid=None, randomized_search=True):
        """
        A light wrapper for Sklearn's linear regression that performs randomized search over a default (and
        overideable) hyperparameter grid.
        """
        if hyperparameter_grid is None:
            # TODO sensible default hyperparameter grid
            pass
            # hyperparameter_grid = {'n_neighbors': neighbor_list, 'weights': ['uniform', 'distance']}

        algorithm = prepare_randomized_search(
            LinearRegression,
            scoring_metric,
            hyperparameter_grid,
            randomized_search)

        algorithm.fit(self.X_train, self.y_train)

        return algorithm

    def knn(self, scoring_metric='roc_auc', hyperparameter_grid=None, randomized_search=True):
        if hyperparameter_grid is None:
            # TODO add sensible KNN hyperparameter grid
            neighbor_list = list(range(10, 26))
            hyperparameter_grid = {'n_neighbors': neighbor_list, 'weights': ['uniform', 'distance']}

        algorithm = prepare_randomized_search(
            KNeighborsClassifier,
            scoring_metric,
            hyperparameter_grid,
            randomized_search,
            n_neighbors=5)

        algorithm.fit(self.X_train, self.y_train)

        return algorithm

    def linear(self, cores=4, debug=False):
        # TODO deprecate
        """
        This method creates and assesses the accuracy of a logistic regression
        model.

        Parameters
        ----------
        cores (num) : Number of cores to use (default 4)
        debug (boolean) : Verbosity of output (default False)

        Returns
        -------
        Nothing. Output to console describes model accuracy.
        """

        if self.model_type == 'classification':
            algo = LogisticRegressionCV(cv=5)
        elif self.model_type == 'regression':
            algo = LinearRegression()
        else:
            algo = None

        self.y_probab_linear, self.au_roc = model_eval.clfreport(
            model_type=self.model_type,
            debug=debug,
            develop_model_mode=True,
            algo=algo,
            X_train=self.X_train,
            y_train=self.y_train,
            X_test=self.X_test,
            y_test=self.y_test,
            cores=cores)

    def random_forest_2(self,
                        trees=200,
                        scoring_metric='roc_auc',
                        hyperparameter_grid=None,
                        randomized_search=True):
        """A convenience method that allows a user to simply call .random_forest() and get the right one."""
        if self.model_type == 'classification':
            self.random_forest_classifier(trees=trees,
                                          scoring_metric=scoring_metric,
                                          hyperparameter_grid=hyperparameter_grid,
                                          randomized_search=randomized_search)
        elif self.model_type == 'regression':
            self.random_forest_regressor(trees=200,
                                         scoring_metric=scoring_metric,
                                         hyperparameter_grid=hyperparameter_grid,
                                         randomized_search=randomized_search)

    def random_forest_classifier(self, trees=200, scoring_metric='roc_auc', hyperparameter_grid=None,
                                 randomized_search=True):
        if hyperparameter_grid is None:
            # TODO add sensible hyperparameter grid
            max_features = helpers.calculate_random_forest_mtry_hyperparameter(len(self.X_test.columns),
                                                                               self.model_type)
            hyperparameter_grid = {'n_estimators': [10, 50, 200], 'max_features': max_features}

        algorithm = prepare_randomized_search(
            RandomForestClassifier,
            scoring_metric,
            hyperparameter_grid,
            randomized_search,
            trees=trees)

        algorithm.fit(self.X_train, self.y_train)

        return algorithm

    def random_forest_regressor(self, trees=200, scoring_metric='roc_auc', hyperparameter_grid=None,
                                randomized_search=True):
        if hyperparameter_grid is None:
            # TODO add sensible hyperparameter grid
            max_features = helpers.calculate_random_forest_mtry_hyperparameter(len(self.X_test.columns),
                                                                               self.model_type)
            hyperparameter_grid = {'n_estimators': [10, 50, 200], 'max_features': max_features}

        algorithm = prepare_randomized_search(
            RandomForestRegressor,
            scoring_metric,
            hyperparameter_grid,
            randomized_search,
            trees=trees)

        algorithm.fit(self.X_train, self.y_train)
        return algorithm

    def random_forest(self, cores=4, trees=200, tune=False, debug=False):
        # TODO deprecate after replacements are implemented.
        """
        This method creates and assesses the accuracy of a logistic regression
        model.

        Parameters
        ----------
        cores (num) : Number of cores to use (default 4)
        trees (num) : Number of trees in the random forest (default 200)
        tune (boolean) : Whether to tune hyperparameters. This iterates number
        of trees from 100, 250, and 500.
        debug (boolean) : Verbosity of output (default False)

        Returns
        -------
        Nothing. Output to console describes model accuracy.
        """

        # TODO: refactor, such that each algo doesn't need an if/else tree
        if self.model_type == 'classification':
            algo = RandomForestClassifier(n_estimators=trees,
                                          verbose=(2 if debug is True else 0))

        elif self.model_type == 'regression':
            algo = RandomForestRegressor(n_estimators=trees,
                                         verbose=(2 if debug is True else 0))

        else:  # Here to appease pep8
            algo = None

        params = {'max_features': helpers.calculate_random_forest_mtry_hyperparameter(len(self.X_test.columns),
                                                                                      self.model_type)}

        self.col_list = self.X_train.columns.values

        self.y_probab_rf, self.au_roc, self.rfclf = model_eval.clfreport(
            model_type=self.model_type,
            debug=debug,
            develop_model_mode=True,
            algo=algo,
            X_train=self.X_train,
            y_train=self.y_train,
            X_test=self.X_test,
            y_test=self.y_test,
            param=params,
            cores=cores,
            tune=tune,
            col_list=self.col_list)

    def plot_roc(self, save=False, debug=False):
        """
        Plots roc related to models resulting from linear and random
        forest methods within the DevelopSupervisedModel step.

        Parameters
        ----------
        save (boolean) : Whether to save the plot
        debug (boolean) : Verbosity of output. If True, shows list of
        FPR/TPR for each point in the plot (default False)

        Returns
        -------
        Nothing. A plot is created and displayed.
        """

        fpr_linear, tpr_linear, _ = roc_curve(self.y_test,
                                              self.y_probab_linear)
        roc_auc_linear = auc(fpr_linear, tpr_linear)

        fpr_rf, tpr_rf, _ = roc_curve(self.y_test, self.y_probab_rf)
        roc_auc_rf = auc(fpr_rf, tpr_rf)

        # TODO: add cutoff associated with FPR/TPR
        if debug:
            print('Linear model:')
            print('FPR, and TRP')
            print(pd.DataFrame(
                {'FPR': fpr_linear, 'TPR': tpr_linear}))

            print('Random forest model:')
            print('FPR, and TRP')
            print(pd.DataFrame({'FPR': fpr_rf, 'TPR': tpr_rf}))

        plt.figure()
        plt.plot(fpr_linear, tpr_linear, color='b',
                 label='Logistic (area = %0.2f)' % roc_auc_linear)
        plt.plot(fpr_rf, tpr_rf, color='g',
                 label='RandomForest (area = %0.2f)' % roc_auc_rf)
        plt.plot([0, 1], [0, 1], 'k--')
        plt.xlim([0.0, 1.0])
        plt.ylim([0.0, 1.05])
        plt.xlabel('False Positive Rate')
        plt.ylabel('True Positive Rate')
        plt.title('Receiver operating characteristic')
        plt.legend(loc="lower right")
        if save:
            plt.savefig('ROC.png')
            source_path = os.path.dirname(os.path.abspath(__file__))
            print('\nROC file saved in: {}'.format(source_path))
            plt.show()
        else:
            plt.show()

    def plot_rffeature_importance(self, save=False):
        """
        Plots feature importances related to models resulting from
        and random forest methods within the DevelopSupervisedModel step.

        Parameters
        ----------
        save (boolean) : Whether to save the plot

        Returns
        -------
        Nothing. A plot is created and displayed.
        """

        # Arrange columns in order of importance
        if hasattr(self.rfclf, 'best_estimator_'):
            importances = self.rfclf.best_estimator_.feature_importances_
            std = np.std(
                [tree.feature_importances_ for tree in
                 self.rfclf.best_estimator_.estimators_],
                axis=0)
        else:
            importances = self.rfclf.feature_importances_
            std = np.std(
                [tree.feature_importances_ for tree in
                 self.rfclf.estimators_],
                axis=0)

        indices = np.argsort(importances)[::-1]
        namelist = [self.col_list[i] for i in indices]

        # Plot these columns
        plt.figure()
        plt.title("Feature importances")
        plt.bar(range(self.X_train.shape[1]),
                importances[indices], color="r",
                yerr=std[indices], align="center")
        plt.xticks(range(self.X_train.shape[1]), namelist, rotation=90)
        plt.xlim([-1, self.X_train.shape[1]])
        plt.gca().set_ylim(bottom=0)
        plt.tight_layout()
        if save:
            plt.savefig('FeatureImportances.png')
            source_path = os.path.dirname(os.path.abspath(__file__))
            print('\nFeature importances saved in: {}'.format(source_path))
            plt.show()
        else:
            plt.show()

    def console_log(self, message):
        if self.verbose:
            print('DSM: {}'.format(message))

    def save_models(self, random_search):
        pass

def prepare_randomized_search(
        estimator,
        scoring_metric,
        hyperparameter_grid,
        randomized_search,
        **non_randomized_estimator_kwargs):
    """
    Given an estimator and various params, initialize an algorithm with optional randomized search.

    Args:
        estimator: a scikit-learn estimator (for example: KNeighborsClassifier)
        scoring_metric: The scoring metric to optimized for if using random search. See
            http://scikit-learn.org/stable/modules/model_evaluation.html
        hyperparameter_grid: An object containing key value pairs of the specific hyperparameter space to search
            through.
        randomized_search (bool): Whether the method should return a randomized search estimator (as opposed to a
            simple algorithm).
        **non_randomized_estimator_kwargs: Keyword arguments that you can pass directly to the algorithm. Only used when
            radomized_search is False

    Returns:
        estimator: a scikit learn algorithm ready to `.fit()`

    """
    if randomized_search:
        algorithm = RandomizedSearchCV(estimator=estimator(),
                                       scoring=scoring_metric,
                                       param_distributions=hyperparameter_grid,
                                       n_iter=2,
                                       cv=5,
                                       verbose=0,
                                       n_jobs=1)

    else:
        print('No randomized search. Using {}'.format(estimator))
        algorithm = estimator(**non_randomized_estimator_kwargs)

    return algorithm
