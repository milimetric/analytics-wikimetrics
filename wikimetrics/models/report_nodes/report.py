import celery
from uuid import uuid4
from celery import current_task
# AsyncResult shows up as un-needed but actually is (for celery.states to work)
from celery.result import AsyncResult
from celery.exceptions import SoftTimeLimitExceeded
from celery.utils.log import get_task_logger
# This is the hack you need if you use instance methods as celery tasks
# from celery.contrib.methods import task_method
from flask.ext.login import current_user
from wikimetrics.configurables import db, queue
from ..persistent_report import PersistentReport


__all__ = [
    'Report',
    'ReportNode',
    'ReportLeaf',
    'queue_task',
]


"""
We use a tree-based report model to represent the relationships
between tasks which have a partial ordering, but which should
still be loosely coupled or asynchronous.  For example, if we want
to compute the average of some metric for a cohort, it makes sense
to first compute the metric for each user and then take the average.
In this example both steps would Reports, and the averaging task would
register the task of computing the actual metric as its child.

Specifically, we distinguish between `ReportLeaf` instances, which have no
subreports, and `ReportNode` isntances, which require their children to be
excecuted first, before carrying out their task.  Computing a simple
metric would be a `ReportLeaf`, whereas any aggregator would be a `ReportNode`
"""


task_logger = get_task_logger(__name__)


@queue.task()
def queue_task(report):
    
    task_logger.info('running {0} on celery as {1}'.format(
        report,
        current_task.request.id,
    ))
    return report.run()


class Report(object):
    
    show_in_ui = False
    task = queue_task
    
    def __init__(self,
                 user_id=None,
                 status=celery.states.PENDING,
                 name=None,
                 queue_result_key=None,
                 children=[],
                 parameters='{}'):
        
        self.user_id = user_id
        if not self.user_id:
            try:
                if current_user.is_authenticated():
                    self.user_id = current_user.id
            except RuntimeError:
                # nothing to worry about, just using current_user outside
                # of a web context.  This should only happen during testing
                pass
        
        self.status = status
        self.name = name
        self.queue_result_key = queue_result_key
        self.children = children
        
        # store report to database
        # note that queue_result_key is always empty at this stage
        pj = PersistentReport(user_id=self.user_id,
                              status=self.status,
                              name=self.name,
                              show_in_ui=self.show_in_ui,
                              parameters=parameters)
        db_session = db.get_session()
        db_session.add(pj)
        db_session.commit()
        self.persistent_id = pj.id
        db_session.close()
    
    def __repr__(self):
        return '<Report("{0}")>'.format(self.persistent_id)
    
    def set_status(self, status, task_id=None):
        """
        helper function for updating database status after celery
        task has been started
        """
        db_session = db.get_session()
        pj = db_session.query(PersistentReport).get(self.persistent_id)
        pj.status = status
        if task_id:
            pj.queue_result_key = task_id
        db_session.add(pj)
        db_session.commit()
        db_session.close()
    
    def run(self):
        """
        each report subclass should implement this method to do the
        meat of the task.  The return type can be anything"""
        pass


class ReportNode(Report):
    
    def run(self):
        """
        This specialized version of run first runs all the children, then
        calls the finish method with the results.
        
        NOTE: this used to spawn a tree of celery tasks.  That sacrificed parallelism
        at the user level and gained parallelism at the task level.  That was bad.
        So now this just runs all the children's run methods, collects the results,
        and passes them to the finish method.  Deadlocking and celery worker starvation
        are *much* less likely now.  Thank you Ori :)
        """
        self.set_status(celery.states.STARTED, task_id=current_task.request.id)
        results = []
        
        if self.children:
            try:
                child_results = [child.run() for child in self.children]
                results = self.finish(child_results)
            except SoftTimeLimitExceeded:
                self.set_status(celery.states.FAILURE)
                task_logger.error('timeout exceeded for {0}'.format(
                    current_task.request.id
                ))
                raise
        
        self.set_status(celery.states.SUCCESS)
        return results
    
    def finish(self, results):
        """
        Each ReportNode sublcass should implement this method to deal with
        the results of its child reports.  As a standard, report_results should
        be called at the end of ReportNode.finish implementations.
        """
        pass
    
    def report_result(self, results, child_results=[]):
        """
        Creates a unique identifier for this ReportNode, and returns a one element
        dictionary with that identifier as the key and its results as the value.
        This allows ReportNode results to be merged as the tree of ReportNodes is
        evaluated.
        
        Parameters
            results         : Anything that the ReportNode compiles in its finish step
            child_results   : The results from a child Report(s) if they should be
                              preserved.  ReportLeaf results and any ReportNode results
                              that are copied should not be preserved.
        """
        self.result_key = str(uuid4())
        db_session = db.get_session()
        pj = db_session.query(PersistentReport).get(self.persistent_id)
        pj.result_key = self.result_key
        db_session.add(pj)
        db_session.commit()
        db_session.close()
        
        merged = {self.result_key: results}
        for child_result in child_results:
            merged.update(child_result)
        return merged


class ReportLeaf(Report):
    pass
