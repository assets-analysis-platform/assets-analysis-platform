import airflow
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

DAG_name = 'examples_wordcount'

main_dag = DAG(
    DAG_name,
    default_args={
        "owner": "Marcin Krolczyk",
        "start_date": airflow.utils.dates.days_ago(1)
    },
    schedule_interval="@daily",
    is_active=False
)

start = PythonOperator(
    task_id="start",
    python_callable=lambda: print("Jobs started"),
    dag=main_dag
)

python_job = SparkSubmitOperator(
    task_id="python_job",
    conn_id="spark-conn",
    application="spark/jobs/sample-project/sample-project-pipeline/python/wordcount_job.py",
    dag=main_dag
)

end = PythonOperator(
    task_id="end",
    python_callable=lambda: print("Jobs completed successfully"),
    dag=main_dag
)

# noinspection PyStatementEffect
start >> python_job >> end

DAGS = [main_dag]
