import airflow
from airflow.decorators import dag
from airflow.operators.python import PythonOperator
from airflow.providers.docker.operators.docker import DockerOperator
from datetime import datetime, timedelta
import logging

DAG_name = 'ethereum_blockchain_extract_on_chain_data'

LOG = logging.getLogger(__name__)


@dag(
    dag_id=DAG_name,
    schedule_interval="0 3 * * *",  # Every day at 03:00 a.m
    start_date=datetime(2022, 11, 21, 3, 0, 0),    # Starting from 21.11.2022, will get data from 21.11.2022 (BTC bottom after the bull market)
    max_active_runs=1,  # max number of active DAG runs in parallel (limited by external API free plan)
    default_args={
        "owner": "mkrolczyk",
        "depends_on_past": True,   # when set to True, task instances will run sequentially while relying on the previous task’s schedule to succeed
        'email_on_failure': False,
        'email_on_retry': False,
        'retries': 3,
        'retry_delay': timedelta(minutes=5),
    }
)
def extract_onchain_data_dag():

    start_processing_info = PythonOperator(
        task_id="start_processing_info",
        provide_context=True,
        python_callable=lambda: LOG.info("Starting Ethereum blockchain ETL job")
    )

    ethereum_etl = DockerOperator(
        task_id="ethereum_etl",
        image="eth-etl:latest",
        docker_url='unix://var/run/docker.sock',
        network_mode='local_assets_analysis_platform',
        command="export_blocks_transactions_and_logs -s {{ ds }} -e {{ ds }} -p {{ var.value.rpc_provider_url }}"
    )

    end_processing_info = PythonOperator(
        task_id="end_processing_info",
        python_callable=lambda: LOG.info("Job completed successfully"),
    )

    start_processing_info >> ethereum_etl >> end_processing_info


main_dag = extract_onchain_data_dag()
DAGS = [main_dag]
