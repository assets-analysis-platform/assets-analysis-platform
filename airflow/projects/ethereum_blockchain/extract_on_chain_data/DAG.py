import os
import logging
from airflow.decorators import dag
from airflow.models import Variable
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator
from airflow.providers.docker.operators.docker import DockerOperator
from airflow.hooks.S3_hook import S3Hook
from airflow.utils.task_group import TaskGroup
from docker.types import Mount
from datetime import datetime, timedelta

DAG_name = 'ethereum_blockchain_extract_on_chain_data'

LOG = logging.getLogger(__name__)


def upload_to_s3(**kwargs) -> None:
    hook = S3Hook('aws-s3-conn')
    key_prefix = ("data/raw/blockchains/ethereum/{directory_name}/date={execution_date}"
                  .format(directory_name=kwargs['data_name'], execution_date=kwargs['ds']))

    for root, dirs, files in os.walk("/output/{key_prefix}".format(key_prefix=key_prefix)):
        for file in files:
            if file.startswith("{file_name}_".format(file_name=kwargs['data_name'])) and file.endswith(".csv"):
                obj_final_key = os.path.join(key_prefix, file)
                LOG.info("Uploading file: {}".format(obj_final_key))
                hook.load_file(
                    filename="/output/{key}".format(key=obj_final_key),     # name of the file to be uploaded from host
                    key=obj_final_key,                                      # AWS S3 destination
                    bucket_name=kwargs['s3_bucket_name']
                )
                LOG.info("Finished uploading file: {}".format(obj_final_key))


@dag(
    dag_id=DAG_name,
    start_date=datetime(2022, 11, 21, 3, 0, 0),
    schedule_interval="0 3 * * *",  # Every day at 03:00 a.m
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
def extract_onchain_data_dag():

    downloaded_data_path = Variable.get("ethereum_etl_downloaded_data_path")

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

    with TaskGroup("upload_chain_data_to_aws_s3") as upload_chain_data_to_aws_s3:
        s3_bucket_name = Variable.get("s3_bucket_name")

        PythonOperator(
            task_id="upload_blocks_data",
            provide_context=True,
            python_callable=upload_to_s3,
            op_kwargs={
                'data_name': 'blocks',
                's3_bucket_name': s3_bucket_name
            }
        )

        PythonOperator(
            task_id="upload_logs_data",
            provide_context=True,
            python_callable=upload_to_s3,
            op_kwargs={
                'data_name': 'logs',
                's3_bucket_name': s3_bucket_name
            }
        )

        PythonOperator(
            task_id="upload_transactions_data",
            provide_context=True,
            python_callable=upload_to_s3,
            op_kwargs={
                'data_name': 'transactions',
                's3_bucket_name': s3_bucket_name
            }
        )

    with TaskGroup("local_env_cleanup") as local_env_cleanup:

        BashOperator(
            task_id="delete_blocks",
            bash_command="rm -rf /output/data/raw/blockchains/ethereum/blocks/date={{ ds }}"
        )

        BashOperator(
            task_id="delete_logs",
            bash_command="rm -rf /output/data/raw/blockchains/ethereum/logs/date={{ ds }}"
        )

        BashOperator(
            task_id="delete_transactions",
            bash_command="rm -rf /output/data/raw/blockchains/ethereum/transactions/date={{ ds }}"
        )

    ethereum_etl >> upload_chain_data_to_aws_s3 >> local_env_cleanup


main_dag = extract_onchain_data_dag()
DAGS = [main_dag]
