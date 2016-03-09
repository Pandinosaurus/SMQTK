#!/usr/bin/env python
import csv
import logging
import math

import matplotlib.pyplot as plt
import numpy
import scipy.stats
import sklearn.metrics

from smqtk.algorithms import (
    get_classifier_impls,
    SupervisedClassifier,
)
from smqtk.representation import (
    ClassificationElementFactory,
    get_descriptor_index_impls,
)
from smqtk.utils import (
    bin_utils,
    parallel,
    plugin,
)


__author__ = "paul.tunison@kitware.com"


def default_config():
    return {
        'plugins': {
            'classifier':
                plugin.make_config(get_classifier_impls()),
            'classification_factory':
                ClassificationElementFactory.get_default_config(),
            'descriptor_index':
                plugin.make_config(get_descriptor_index_impls())
        },
        'utility': {
            'train': False,
            'csv_filepath': 'CHAMGEME :: PATH :: a csv file',
            'output_plot_pr': None,
            'output_plot_roc': None,
            'curve_confidence_interval': True,
            'curve_confidence_interval_alpha': 0.4,
        },
        "parallelism": {
            "descriptor_fetch_cores": 4,
            "classification_cores": None,
        },
    }


def main():
    description = """
    Utility for validating a given classifier implementation's model against
    some labeled testing data, outputting PR and ROC curve plots with
    area-under-curve score values.

    This utility can optionally be used train a supervised classifier model if
    the given classifier model configuration does not exist and a second CSV
    file listing labeled training data is provided. Training will be attempted
    if ``train`` is set to true. If training is performed, we exit after
    training completes. A ``SupervisedClassifier`` sub-classing implementation
    must be configured

    We expect the test and train CSV files in the column format:

        ...
        <UUID>,<label>
        ...

    The UUID is of the descriptor to which the label applies. The label may be
    any arbitrary string value, but all labels must be consistent in
    application.
    """
    args, config = bin_utils.utility_main_helper(default_config, description)
    log = logging.getLogger(__name__)

    #
    # Initialize stuff from configuration
    #
    #: :type: smqtk.algorithms.Classifier
    classifier = plugin.from_plugin_config(
        config['plugins']['classifier'],
        get_classifier_impls()
    )
    #: :type: ClassificationElementFactory
    classification_factory = ClassificationElementFactory.from_config(
        config['plugins']['classification_factory']
    )
    #: :type: smqtk.representation.DescriptorIndex
    descriptor_index = plugin.from_plugin_config(
        config['plugins']['descriptor_index'],
        get_descriptor_index_impls()
    )

    uuid2label_filepath = config['utility']['csv_filepath']
    do_train = config['utility']['train']
    plot_filepath_pr = config['utility']['output_plot_pr']
    plot_filepath_roc = config['utility']['output_plot_roc']
    plot_ci = config['utility']['curve_confidence_interval']
    plot_ci_alpha = config['utility']['curve_confidence_interval_alpha']

    #
    # Construct mapping of label to the DescriptorElement instances for that
    # described by that label.
    #
    log.info("Loading descriptors by UUID")

    def iter_uuid_label():
        """ Iterate through UUIDs in specified file """
        with open(uuid2label_filepath) as uuid2label_file:
            reader = csv.reader(uuid2label_file)
            for r in reader:
                # TODO: This will need to be updated to handle multiple labels
                #       per descriptor.
                yield r[0], r[1]

    def get_descr(r):
        """ Fetch descriptors from configured index """
        uuid, label = r
        return label, descriptor_index.get_descriptor(uuid)

    label_element_iter = parallel.parallel_map(
        get_descr, iter_uuid_label(),
        name="cmv_get_descriptors",
        use_multiprocessing=True,
        cores=config['parallelism']['descriptor_fetch_cores'],
    )

    #: :type: dict[str, list[smqtk.representation.DescriptorElement]]
    label2descriptors = {}
    for label, d in label_element_iter:
        label2descriptors.setdefault(label, []).append(d)

    # Train classifier if the one given has a ``train`` method and training
    # was turned enabled.
    if do_train:
        if isinstance(classifier, SupervisedClassifier):
            log.info("Training classifier model")
            classifier.train(label2descriptors)
            exit(0)
        else:
            ValueError("Configured classifier is not a SupervisedClassifier "
                       "type and does not support training.")

    #
    # Apply classifier to descriptors for predictions
    #
    #: :type: dict[str, set[smqtk.representation.ClassificationElement]]
    label2classifications = {}
    for label, descriptors in label2descriptors.iteritems():
        label2classifications[label] = \
            set(classifier.classify_async(
                descriptors, classification_factory,
                use_multiprocessing=True,
                procs=config['parallelism']['classification_cores'],
                ri=1.0,
            ).values())

    #
    # Create PR/ROC curves via scikit learn tools
    #
    if plot_filepath_pr:
        log.info("Making PR curve")
        make_pr_curves(label2classifications, plot_filepath_pr,
                       plot_ci, plot_ci_alpha)
    if plot_filepath_roc:
        log.info("Making ROC curve")
        make_roc_curves(label2classifications, plot_filepath_roc,
                        plot_ci, plot_ci_alpha)


def format_plt(title, x_label, y_label):
    plt.xlim([0., 1.])
    plt.ylim([0., 1.05])
    plt.xlabel(x_label)
    plt.ylabel(y_label)
    plt.title(title)
    plt.legend(loc='best', fancybox=True, framealpha=0.5)


def select_color_marker(i):
    colors = ['b', 'g', 'r', 'c', 'y', 'k']
    style = ['-', '--', '-.', ':']
    ci = i % len(colors)
    si = (i // len(colors)) % len(style)
    return '%s%s' % (colors[ci], style[si])


def make_curve(log, skl_curve_func, title, xlabel, ylabel, output_filepath,
               label2classifications, plot_ci, plot_ci_alpha):
    """
    :param skl_curve_func: scikit-learn curve generation function. This should
        be wrapped to return (x, y) value arrays of the curve plot.
    :type skl_curve_func:
        (list[float], list[float]) -> (numpy.ndarray[float], numpy.ndarray[float], numpy.ndarray[float])

    :param label2classifications: Mapping of label to the classification
        elements that should be that label.
    :type label2classifications:
        dict[str, set[smqtk.representation.ClassificationElement]]

    """
    # Create curves for each label and then for overall.

    all_classifications = set()
    for s in label2classifications.values():
        all_classifications.update(s)

    # collection of all binary truth-probability pairs
    g_truth = []
    g_proba = []

    plt.clf()
    plt.figure(figsize=(15, 12))
    line_i = 0
    for l in label2classifications:
        # record binary truth relation with respect to label `l`
        l_truth = []
        l_proba = []
        for c in all_classifications:
            l_truth.append(int(c in label2classifications[l]))
            l_proba.append(c[l])
        assert len(l_truth) == len(l_proba) == len(all_classifications)
        x, y, t = skl_curve_func(numpy.array(l_truth), numpy.array(l_proba))
        auc = sklearn.metrics.auc(x, y)
        m = select_color_marker(line_i)
        plt.plot(x, y, m, label="%s (auc=%f)" % (l, auc))

        if plot_ci:
            # Confidence interval calculation using Wilson's score interval
            x_u, x_l = curve_wilson_ci(x, len(l_proba))
            y_u, y_l = curve_wilson_ci(y, len(l_proba))
            ci_poly = plt.Polygon(zip(x_l, y_l) + zip(reversed(x_u), reversed(y_u)),
                                  facecolor=m[0], edgecolor=m[0],
                                  alpha=plot_ci_alpha)
            plt.gca().add_patch(ci_poly)

        g_truth.extend(l_truth)
        g_proba.extend(l_proba)

        line_i += 1

    x, y, t = skl_curve_func(numpy.array(g_truth), numpy.array(g_proba))
    auc = sklearn.metrics.auc(x, y)
    m = select_color_marker(line_i)
    plt.plot(x, y, m, label="Overall (auc=%f)" % auc)

    if plot_ci:
        # Confidence interval generation
        x_u, x_l = curve_wilson_ci(x, len(g_proba))
        y_u, y_l = curve_wilson_ci(y, len(g_proba))
        ci_poly = plt.Polygon(zip(x_l, y_l) + zip(reversed(x_u), reversed(y_u)),
                              facecolor=m[0], edgecolor=m[0],
                              alpha=plot_ci_alpha)
        plt.gca().add_patch(ci_poly)

    format_plt(title, xlabel, ylabel)
    plt.savefig(output_filepath)


def make_pr_curves(label2classifications, output_filepath, plot_ci,
                   plot_ci_alpha):
    def skl_pr_curve(truth, proba):
        p, r, t = sklearn.metrics.precision_recall_curve(truth, proba,
                                                         pos_label=1)
        return r, p, t

    log = logging.getLogger(__name__)
    make_curve(log, skl_pr_curve, "PR", "Recall", "Precision", output_filepath,
               label2classifications, plot_ci, plot_ci_alpha)


def make_roc_curves(label2classifications, output_filepath, plot_ci,
                    plot_ci_alpha):
    def skl_roc_curve(truth, proba):
        fpr, tpr, t = sklearn.metrics.roc_curve(truth, proba, pos_label=1)
        return fpr, tpr, t

    log = logging.getLogger(__name__)
    make_curve(log, skl_roc_curve, "ROC", "False Positive Rate",
               "True Positive Rate", output_filepath, label2classifications,
               plot_ci, plot_ci_alpha)


def curve_wilson_ci(p, n, confidence=0.95):
    """
    Generate upper and lower bounds confidence interval, using the Wilson score
    interval, for PR and ROC curves.

    :param p: Array of points for an axis.
    :type p: numpy.ndarray[float]

    :param n: number of source predictions that led to the ``p`` array.
    :type n: int

    :param confidence: (0, 1) confidence value for interval generation. Default
        of 0.95, or 95% confidence bounds.
    :type confidence: float

    :return: Upper and lower bounds arrays.
    :rtype: (numpy.ndarray[float], numpy.ndarray[float])

    """
    z = scipy.stats.norm.ppf(1-(1-float(confidence))/2)
    n = float(n)
    u = (1/(1+(z*z/n))) * \
        (p + (z*z)/(2*n) + z*numpy.sqrt(p * (1 - p) / n + (z*z)/(4*n*n)))
    l = (1/(1+(z*z/n))) * \
        (p + (z*z)/(2*n) - z*numpy.sqrt(p * (1 - p) / n + (z*z)/(4*n*n)))
    return u, l


if __name__ == '__main__':
    main()
