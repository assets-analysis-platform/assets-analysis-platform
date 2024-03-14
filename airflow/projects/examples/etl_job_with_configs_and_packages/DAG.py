import airflow
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

DAG_name = 'examples_etl_job_with_configs_and_packages'

main_dag = DAG(
    DAG_name,
    default_args={
        "owner": "Marcin Krolczyk",
        "start_date": airflow.utils.dates.days_ago(1)
    },
    schedule_interval="@daily"
)

start = PythonOperator(
    task_id="start",
    python_callable=lambda: print("Job started"),
    dag=main_dag
)

python_job = SparkSubmitOperator(
    task_id="python_job",
    conn_id="spark-conn",
    py_files="spark/packages.zip",
    application="spark/jobs/sample-project/sample-project-pipeline/python/etl_job.py",
    files="spark/configs/sample-project/sample-project-pipeline/etl_job_config.json",
    dag=main_dag
)

end = PythonOperator(
    task_id="end",
    python_callable=lambda: print("Job completed successfully"),
    dag=main_dag
)

# noinspection PyStatementEffect
start >> python_job >> end

DAGS = [main_dag]
