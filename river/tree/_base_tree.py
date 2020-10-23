import typing
from abc import ABC, abstractmethod

import math

from river.utils.skmultiflow_utils import calculate_object_size

from ._nodes import Node
from ._nodes import LearningNode
from ._nodes import ActiveLeaf
from ._nodes import InactiveLeaf
from ._nodes import SplitNode
from ._nodes import FoundNode
from ._attribute_test import InstanceConditionalTest


class DecisionTree(ABC):
    """Base class for Decision Trees.

    It defines base operations and properties that all the decision trees must inherit or
    implement according to their own design.

    All the extended classes inherit the following functionality:

    * Set the maximum tree depth allowed (`max_depth`).
    * Handle *Active* and *Inactive* nodes: Active learning nodes update their own
    internal state to improve predictions and monitor input features to perform split
    attempts. Inactive learning nodes do not update their internal state and only keep the
    predictors; they are used to save memory in the tree (`max_size`).
    *  Enable/disable memory management.
    * Define strategies to sort leaves according to how likely they are going to be split.
    This enables deactivating non-promising leaves to save memory.
    * Disabling ‘poor’ attributes to save memory and speed up tree construction.
    A poor attribute is an input feature whose split merit is much smaller than the current
    best candidate. Once a feature is disabled, the tree stops saving statistics necessary
    to split such a feature.
    * Define properties to access leaf prediction strategies, split criteria, and other
    relevant characteristics.

    Parameters
    ----------
    max_depth
        The maximum depth a tree can reach. If `None`, the tree will grow indefinitely.
    binary_split
        If True, only allow binary splits.
    max_size
        The max size of the tree, in Megabytes (MB).
    memory_estimate_period
        Interval (number of processed instances) between memory consumption checks.
    stop_mem_management
        If True, stop growing as soon as memory limit is hit.
    remove_poor_atts
        If True, disable poor attributes to reduce memory usage.
    merit_preprune
        If True, enable merit-based tree pre-pruning.
    """
    def __init__(self, max_depth: int = None, binary_split: bool = False, max_size: int = 100,
                 memory_estimate_period: int = 1000000, stop_mem_management: bool = False,
                 remove_poor_atts: bool = False, merit_preprune: bool = True):
        self.max_depth = max_depth if max_depth is not None else math.inf
        self.binary_split = binary_split
        self._max_size = max_size
        self._max_byte_size = self._max_size * (2 ** 20)  # convert to byte
        self.memory_estimate_period = memory_estimate_period
        self.stop_mem_management = stop_mem_management
        self.remove_poor_atts = remove_poor_atts
        self.merit_preprune = merit_preprune

        self._tree_root = None
        self._n_decision_nodes = 0
        self._n_active_leaves = 0
        self._n_inactive_leaves = 0
        self._inactive_leaf_size_estimate = 0.0
        self._active_leaf_size_estimate = 0.0
        self._size_estimate_overhead_fraction = 1.0
        self._growth_allowed = True
        self._train_weight_seen_by_model = 0.0

    @staticmethod
    def _hoeffding_bound(range_val, confidence, n):
        r"""Compute the Hoeffding bound, used to decide how many samples are necessary at each
        node.

        Notes
        -----
        The Hoeffding bound is defined as:

        $\\epsilon = \\sqrt{\\frac{R^2\\ln(1/\\delta))}{2n}}$

        where:

        $\\epsilon$: Hoeffding bound.
        $R$: Range of a random variable. For a probability the range is 1, and for an
        information gain the range is log *c*, where *c* is the number of classes.
        $\\delta$: Confidence. 1 minus the desired probability of choosing the correct
        attribute at any given node.
        $n$: Number of samples.

        Parameters
        ----------
        range_val
            Range value.
        confidence
            Confidence of choosing the correct attribute.
        n
            Number of processed samples.
        """
        return math.sqrt((range_val * range_val * math.log(1. / confidence)) / (2. * n))

    @property
    def max_size(self):
        return self._max_size

    @max_size.setter
    def max_size(self, size):
        self._max_size = size
        self._max_byte_size = self._max_size * (2 ** 20)

    @property
    def model_measurements(self):
        """Collect metrics corresponding to the current status of the tree.

        Returns
        -------
        string
            A string buffer containing the measurements of the tree.
        """
        measurements = {'Tree size (nodes)': self._n_decision_nodes
                        + self._n_active_leaves + self._n_inactive_leaves,
                        'Tree size (leaves)': self._n_active_leaves
                        + self._n_inactive_leaves,
                        'Active learning nodes': self._n_active_leaves,
                        'Tree depth': self.depth,
                        'Active leaf byte size estimate': self._active_leaf_size_estimate,
                        'Inactive leaf byte size estimate': self._inactive_leaf_size_estimate,
                        'Byte size estimate overhead': self._size_estimate_overhead_fraction
                        }
        return measurements

    def get_model_description(self):
        """Walk the tree and return its structure in a buffer.

        Returns
        -------
        string
            The description of the model.

        """
        if self._tree_root is not None:
            buffer = ['']
            description = ''
            self._tree_root.describe_subtree(self, buffer, 0)
            for line in range(len(buffer)):
                description += buffer[line]
            return description

    def _new_split_node(self, split_test: InstanceConditionalTest, target_stats: dict = None,
                        depth: int = 0) -> SplitNode:
        """Create a new split node."""
        return SplitNode(split_test, target_stats, depth)

    @abstractmethod
    def _new_learning_node(self, initial_stats: dict = None, parent: LearningNode = None,
                           is_active: bool = True) -> LearningNode:
        """Create a new learning node.

        The characteristics of the learning node depends on the tree algorithm.

        Parameters
        ----------
        initial_stats
            Target statistics set from the parent node.
        parent
            Parent node to inherit from.
        is_active
            Define whether or not the new node to be created is an active learning node.

        Returns
        -------
            A new learning node.
        """

    @property
    def depth(self) -> int:
        """Calculate the depth of the tree.

        Returns
        -------
        int
            Depth of the tree.
        """
        if isinstance(self._tree_root, Node):
            return self._tree_root.subtree_depth()
        return 0

    @property
    def split_criterion(self) -> str:
        """Return a string with the name of the split criterion being used by the tree. """
        return self._split_criterion

    @split_criterion.setter
    @abstractmethod
    def split_criterion(self, split_criterion):
        """Define the split criterion to be used by the tree. """

    @property
    def leaf_prediction(self) -> str:
        """Return the prediction strategy used by the tree at its leaves. """
        return self._leaf_prediction

    @leaf_prediction.setter
    @abstractmethod
    def leaf_prediction(self, leaf_prediction):
        """Define the prediction strategy used by the tree in its leaves."""

    def _enforce_size_limit(self):
        """Track the size of the tree and disable/enable nodes if required."""
        tree_size = (self._active_leaf_size_estimate
                     + self._n_inactive_leaves * self._inactive_leaf_size_estimate) \
            * self._size_estimate_overhead_fraction
        if self._n_inactive_leaves > 0 or tree_size > self._max_byte_size:
            if self.stop_mem_management:
                self._growth_allowed = False
                return
        learning_nodes = self._find_learning_nodes()
        learning_nodes.sort(key=lambda n: n.node.calculate_promise())
        max_active = 0
        while max_active < len(learning_nodes):
            max_active += 1
            if (((max_active * self._active_leaf_size_estimate
                    + (len(learning_nodes) - max_active) * self._inactive_leaf_size_estimate)
                    * self._size_estimate_overhead_fraction) > self._max_byte_size):
                max_active -= 1
                break
        cutoff = len(learning_nodes) - max_active
        for i in range(cutoff):
            if isinstance(learning_nodes[i].node, ActiveLeaf):
                self._deactivate_leaf(learning_nodes[i].node,
                                      learning_nodes[i].parent,
                                      learning_nodes[i].parent_branch)
        for i in range(cutoff, len(learning_nodes)):
            if isinstance(learning_nodes[i].node, InactiveLeaf) and learning_nodes[i].node.depth \
                    < self.max_depth:
                self._activate_leaf(learning_nodes[i].node, learning_nodes[i].parent,
                                    learning_nodes[i].parent_branch)

    def _estimate_model_size(self):
        """Calculate the size of the model and trigger tracker function
        if the actual model size exceeds the max size in the configuration."""
        learning_nodes = self._find_learning_nodes()
        total_active_size = 0
        total_inactive_size = 0
        for found_node in learning_nodes:
            if not found_node.node.is_leaf():  # Safety check for non-trivial tree structures
                continue
            if isinstance(found_node.node, ActiveLeaf):
                total_active_size += calculate_object_size(found_node.node)
            else:
                total_inactive_size += calculate_object_size(found_node.node)
        if total_active_size > 0:
            self._active_leaf_size_estimate = total_active_size / self._n_active_leaves
        if total_inactive_size > 0:
            self._inactive_leaf_size_estimate = total_inactive_size \
                / self._n_inactive_leaves
        actual_model_size = calculate_object_size(self)
        estimated_model_size = (self._n_active_leaves * self._active_leaf_size_estimate
                                + self._n_inactive_leaves
                                * self._inactive_leaf_size_estimate)
        self._size_estimate_overhead_fraction = actual_model_size / estimated_model_size
        if actual_model_size > self._max_byte_size:
            self._enforce_size_limit()

    def _deactivate_all_leaves(self):
        """Deactivate all leaves. """
        learning_nodes = self._find_learning_nodes()
        for cur_node in learning_nodes:
            if isinstance(cur_node, ActiveLeaf):
                self._deactivate_leaf(cur_node.node, cur_node.parent, cur_node.parent_branch)

    def _deactivate_leaf(self, to_deactivate: ActiveLeaf, parent: SplitNode, parent_branch: int):
        """Deactivate a learning node.

        Parameters
        ----------
        to_deactivate
            The node to deactivate.
        parent
            The node's parent.
        parent_branch
            Parent node's branch index.
        """

        # We pass the active learning node as parent to ensure its properties are accessible
        # to perform possible transfers or copies (as it happens in the regression case)
        new_leaf = self._new_learning_node(to_deactivate.stats, parent=to_deactivate,
                                           is_active=False)
        new_leaf.depth -= 1  # To ensure we do not skip a tree level
        if parent is None:
            self._tree_root = new_leaf
        else:
            parent.set_child(parent_branch, new_leaf)
        self._n_active_leaves -= 1
        self._n_inactive_leaves += 1

    def _activate_leaf(self, to_activate: InactiveLeaf, parent: SplitNode, parent_branch: int):
        """Activate a learning node.

        Parameters
        ----------
        to_activate
            The node to activate.
        parent
            The node's parent.
        parent_branch
            Parent node's branch index.
        """
        new_leaf = self._new_learning_node(to_activate.stats, parent=to_activate)
        new_leaf.depth -= 1
        if parent is None:
            self._tree_root = new_leaf
        else:
            parent.set_child(parent_branch, new_leaf)
        self._n_active_leaves += 1
        self._n_inactive_leaves -= 1

    def _find_learning_nodes(self) -> typing.List[FoundNode]:
        """Find learning nodes in the tree.

        Returns
        -------
        list
            List of learning nodes in the tree.
        """
        found_list = []
        self.__find_learning_nodes(self._tree_root, None, -1, found_list)
        return found_list

    def __find_learning_nodes(self, node, parent, parent_branch, found):
        """Find learning nodes in the tree from a given node.

        Parameters
        ----------
        node
            The node to start the search.
        parent
            The node's parent.
        parent_branch
            Parent node's branch.
        found
            A list of found nodes.

        Returns
        -------
        list
            List of learning nodes.
        """
        if node is not None:
            if isinstance(node, LearningNode):
                found.append(FoundNode(node, parent, parent_branch))
            if isinstance(node, SplitNode):
                split_node = node
                for i in range(split_node.n_children):
                    self.__find_learning_nodes(
                        split_node.get_child(i), split_node, i, found)