# Airflow

Airflow provisioning and deployment code

## First set up guide

### Create and activate virtual ENV

Linux
1. Make sure you're in ```airflow``` directory and type:
2. ```$ py -m virtualenv aap_airflow_virtualenv```
3. ```$ source ./aap_airflow_virtualenv/Scripts/activate```
4. ```(aap_airflow_virtualenv) $ pip install -r requirements.txt```

Windows
1. Make sure you're in ```airflow``` directory and type:
2. ```$ py -m virtualenv aap_airflow_virtualenv```
3. ```$ .\aap_airflow_virtualenv\Scripts\activate```
4. ```(aap_airflow_virtualenv) $ pip install -r requirements.txt```

### Deactivate virtual ENV

Linux \
```(aap_airflow_virtualenv) $ source ./aap_airflow_virtualenv/Scripts/deactivate```

Windows \
```(aap_airflow_virtualenv) $ .\aap_airflow_virtualenv\Scripts\deactivate```

### Activate virtual ENV in IntelliJ Idea
- https://www.jetbrains.com/help/idea/creating-virtual-environment.html

### Update 'requirements.txt' (if new packages installed)
```(aap_airflow_virtualenv) $ pip freeze > requirements.txt```

## References
- https://towardsdatascience.com/airflow-design-pattern-to-manage-multiple-airflow-projects-e695e184201b