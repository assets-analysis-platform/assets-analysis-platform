import logging
from airflow.decorators import dag
from airflow.models import Variable
from airflow.operators.python import PythonOperator
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
from datetime import datetime, timedelta

DAG_name = 'uniswap_exchange_aggr_ur_swap_events_data'

LOG = logging.getLogger(__name__)


def prepare_input(**kwargs):
    ti = kwargs['ti']

    s3_bucket_name = Variable.get("s3_bucket_name")
    output_base_uri = f's3a://{s3_bucket_name}/data/extracted/transactions/uniswap/universal-router'

    start_date = datetime.strptime(kwargs['dag_run'].conf['start_date'], '%Y-%m-%d').date()   # result is 'YYYY-MM-DD' format
    end_date = datetime.strptime(kwargs['dag_run'].conf['end_date'], '%Y-%m-%d').date()       # same as above

    paths_list = [
        f'{output_base_uri}/date={start_date + timedelta(days=i)}/ur_transactions_{start_date + timedelta(days=i)}/part-*.csv'
        for i in range((end_date - start_date).days + 1)
    ]
    csv_files_uris_str = ','.join(paths_list)

    LOG.info('Successfully retrieved .csv URIs for <{}, {}> date range'.format(start_date, end_date))

    ti.xcom_push(key='input_csv_files_uris', value=csv_files_uris_str)
    ti.xcom_push(key='s3_output_uri', value=f'{output_base_uri}/swaps/swaps_from_{start_date}_to_{end_date}')
    ti.xcom_push(key='s3_failed_rows_output_uri', value=f'{output_base_uri}/swaps/swaps_from_{start_date}_to_{end_date}_failed_rows')


@dag(
    dag_id=DAG_name,
    schedule_interval=None,  # Triggered manually
    max_active_runs=1,
    default_args={
        "owner": "mkrolczyk",
        "depends_on_past": True,
        'email_on_failure': False,
        'email_on_retry': False,
        'retries': 3,
        'retry_delay': timedelta(minutes=5),
    }
)
def aggr_ur_swap_events_data_dag():

    prepare_input_for_ur_aggr_job = PythonOperator(
        task_id="prepare_input_for_ur_aggr_job",
        provide_context=True,
        python_callable=prepare_input
    )

    aggr_ur_swap_events_data_spark_job = SparkSubmitOperator(
        task_id="aggr_ur_swap_events_data_spark_job",
        conn_id="spark-conn",
        py_files="spark/packages.zip",
        packages='org.apache.spark:spark-hadoop-cloud_2.12:3.3.2',
        application="spark/jobs/uniswap-exchange/aggr-ur-swap-events-data-job/python/aggr_ur_swap_events_data_etl_job.py",
        application_args=[
            "{{ ti.xcom_pull(key='input_csv_files_uris') }}",
            "{{ ti.xcom_pull(key='s3_output_uri') }}",
            "{{ ti.xcom_pull(key='s3_failed_rows_output_uri') }}"
        ]
    )

    prepare_input_for_ur_aggr_job >> aggr_ur_swap_events_data_spark_job


main_dag = aggr_ur_swap_events_data_dag()
DAGS = [main_dag]
