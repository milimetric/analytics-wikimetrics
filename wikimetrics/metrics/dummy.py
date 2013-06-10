import random
from metric import Metric
import logging

__all__ = ['RandomMetric']

logger = logging.getLogger(__name__)

class RandomMetric(Metric):
    def __call__(self, cohort):
        logger.debug('globals(): %s', globals())
        return {user:random.random() for user in cohort}