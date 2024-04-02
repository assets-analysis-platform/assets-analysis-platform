import logging
from airflow.decorators import dag
from airflow.models import Variable
from airflow.operators.python import PythonOperator
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
from datetime import datetime, timedelta

DAG_name = 'uniswap_exchange_extract_ur_transactions'

LOG = logging.getLogger(__name__)


def prepare_args_for_spark(**kwargs):

    ti = kwargs['ti']
    transactions_date = kwargs['yesterday_ds']

    s3_bucket_name = Variable.get("s3_bucket_name")
    input_base_uri = f's3a://{s3_bucket_name}/data/raw/blockchains/ethereum'
    output_base_uri = f's3a://{s3_bucket_name}/data/extracted/transactions/uniswap/universal-router'

    ti.xcom_push(key='s3_eth_transactions_uri', value=f'{input_base_uri}/transactions/date={transactions_date}/transactions_*.csv')
    ti.xcom_push(key='s3_eth_logs_uri', value=f'{input_base_uri}/logs/date={transactions_date}/logs_*.csv')
    ti.xcom_push(key='s3_output_uri', value=f'{output_base_uri}/date={transactions_date}/ur_transactions_{transactions_date}.csv')


@dag(
    dag_id=DAG_name,
    start_date=datetime(2023, 5, 9, 4, 0, 0),
    schedule_interval="0 4 * * *",  # Every day at 04:00 a.m
    max_active_runs=1,              # max number of active DAG runs in parallel
    default_args={
        "owner": "mkrolczyk",
        "depends_on_past": True,    # when set to True, task instances will run sequentially while relying on the previous task’s schedule to succeed
        'email_on_failure': False,
        'email_on_retry': False,
        'retries': 3,
        'retry_delay': timedelta(minutes=5),
    }
)
def extract_uniswap_ur_transactions_dag():

    get_args_for_ur_spark_job = PythonOperator(
        task_id="get_args_for_ur_spark_job",
        provide_context=True,
        python_callable=prepare_args_for_spark
    )

    extract_ur_transactions_spark_job = SparkSubmitOperator(
        task_id="extract_ur_transactions_spark_job",
        conn_id="spark-conn",
        py_files="spark/packages.zip",
        files="spark/configs/uniswap-exchange/extract-ur-transactions-pipeline/extract_ur_data_etl_job_config.json",
        packages='org.apache.spark:spark-hadoop-cloud_2.12:3.3.2',
        application="spark/jobs/uniswap-exchange/extract-ur-transactions-pipeline/python/extract_ur_data_etl_job.py",
        application_args=[
            "{{ ti.xcom_pull(key='s3_eth_transactions_uri') }}",
            "{{ ti.xcom_pull(key='s3_eth_logs_uri') }}",
            "{{ ti.xcom_pull(key='s3_output_uri') }}"
        ]
    )

    get_args_for_ur_spark_job >> extract_ur_transactions_spark_job


main_dag = extract_uniswap_ur_transactions_dag()
DAGS = [main_dag]
