import os
import sys
import traceback

from importlib import import_module

# get airflow base dir
AIRFLOW_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(AIRFLOW_BASE)


class DAGFactory(object):
    """
        Dag factory: collect dags from all sub folders under ./airflow/projects
    """

    def __init__(self):
        self.project_base = os.path.join(AIRFLOW_BASE, 'projects')

    @property
    def projects(self):
        return list(
            filter(lambda x: x not in ['__pycache__', '.idea'],
                   [dirs for _, dirs, _ in os.walk(self.project_base)][0]))

    def get_modules_from_all_projects(self):

        for project_name in self.projects:
            project_path = os.path.join(self.project_base, project_name)

            for subpipeline_name in os.listdir(project_path):
                subpipeline_path = os.path.join(project_path, subpipeline_name)
                dag_module_path = os.path.join(subpipeline_path, 'DAG.py')

                if os.path.exists(dag_module_path):
                    try:
                        module_name = "projects.{}.{}.DAG".format(project_name, subpipeline_name)
                        prj_mod = import_module(module_name)
                        no_of_project_dags = 1

                        for dag in prj_mod.DAGS:
                            var_name = '{}-{}'.format(subpipeline_name, no_of_project_dags)

                            # create unique name for project in global namespace
                            globals()[var_name] = dag

                            no_of_project_dags += 1
                    except Exception:
                        print(traceback.format_exc())


dags = DAGFactory()

print("Loading DAGs from projects: {}".format(dags.projects))
dags.get_modules_from_all_projects()
print("DAGs has been successfully loaded")
