from flask import render_template, request, url_for
from flask.ext.login import current_user
import celery
from ..configurables import app, db
from ..models import Cohort, Job, JobResponse, PersistentJob, MultiProjectMetricJob
from ..metrics import metric_classes
from ..utils import json_response, json_redirect
import json


@app.route('/jobs/')
def jobs_index():
    """
    Renders a page with a list of jobs started by the currently logged in user.
    If the user is an admin, she has the option to see other users' jobs.
    """
    return render_template('jobs.html')


@app.route('/jobs/create/', methods=['GET', 'POST'])
def jobs_request():
    """
    Renders a page that facilitates kicking off a new job
    """
    
    if request.method == 'GET':
        return render_template('request.html')
    else:
        parsed = json.loads(request.form['responses'])
        metric_jobs = []
        for cohort_metric_dict in parsed:
            app.logger.debug('cohort_metric_dict: %s', cohort_metric_dict)
            
            # get cohort
            cohort_dict = cohort_metric_dict['cohort']
            db_session = db.get_session()
            # TODO: filter by current_user
            cohort = db_session.query(Cohort).get(cohort_dict['id'])
            app.logger.debug('cohort: %s', cohort)
            
            # construct metric
            metric_dict = cohort_metric_dict['metric']
            class_name = metric_dict.pop('name')
            metric_class = metric_classes[class_name]
            metric = metric_class(**metric_dict)
            metric.validate()
            app.logger.debug('metric: %s', metric)
            
            # construct and start JobResponse
            metric_job = MultiProjectMetricJob(cohort, metric, cohort_metric_dict['name'])
            metric_jobs.append(metric_job)
        jr = JobResponse(metric_jobs)
        async_response = jr.task.delay()
        app.logger.info('starting job: %s', async_response.task_id)
            
        #return render_template('jobs.html')
        return json_redirect(url_for('jobs_index'))


@app.route('/jobs/list/')
def jobs_list():
    db_session = db.get_session()
    jobs = db_session.query(PersistentJob)\
        .filter_by(user_id=current_user.id).all()
    # update status for each job
    for job in jobs:
        job.update_status()
    # TODO fix json_response to deal with PersistentJob objects
    return json_response(jobs=[job._asdict() for job in jobs])


@app.route('/jobs/status/<job_id>')
def job_status(job_id):
    db_session = db.get_session()
    db_job = db_session.query(PersistentJob).get(job_id)
    if db_job.status not in (celery.states.SUCCESS, celery.states.FAILURE):
        if db_job.result_key:
            # if we don't have the result key leave as is (PENDING)
            celery_task = Job.task.AsyncResult(db_job.result_key)
            db_job.status = celery_task.status
            db_session.add(db_job)
            db_session.commit()
    return json_response(status=db_job.status)
