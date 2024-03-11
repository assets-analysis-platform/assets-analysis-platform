from airflow.decorators import dag
from airflow.models import Variable
from airflow.operators.python import PythonOperator
from airflow.providers.docker.operators.docker import DockerOperator
from docker.types import Mount
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

    downloaded_data_path = Variable.get("ethereum_etl_downloaded_data_path")

    start_processing_info = PythonOperator(
        task_id="start_processing_info",
        provide_context=True,
        python_callable=lambda: LOG.info("Starting Ethereum blockchain ETL job")
    )

    ethereum_etl = DockerOperator(
        task_id="ethereum_etl",
        image="eth-etl:latest",
        docker_url=Variable.get("docker_socket_url"),
        auto_remove="success",
        mounts=[
            Mount(
                source=downloaded_data_path,
                target="/ethereum-etl/output",
                type='bind'
            )
        ],
        network_mode=Variable.get("aap_cluster_network_name"),
        command="export_blocks_transactions_and_logs "
                "--start {{ ds }} "
                "--end {{ ds }} "
                "--provider-uri {{ var.value.rpc_provider_url }}"
    )

    end_processing_info = PythonOperator(
        task_id="end_processing_info",
        python_callable=lambda: LOG.info(downloaded_data_path),
    )

    start_processing_info >> ethereum_etl >> end_processing_info


main_dag = extract_onchain_data_dag()
DAGS = [main_dag]
