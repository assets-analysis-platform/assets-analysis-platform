# Extract Universal Router transactions pipeline

The purpose of this pipeline is to filter Ethereum blockchain data in order to extract data that comes 
from the Universal Router contract from the decentralized Uniswap exchange. 
The obtained raw data is then processed in the Apache Spark cluster to extract detailed information 
about transactions from the past.

### Pipeline architecture
![architecture.png](docs/architecture.png)

### Components
* Airflow scheduler - Manages and monitors the flow of tasks, triggers the task instances whose dependencies have been met.
* Apache Spark cluster - Data processing framework used to process Ethereum Blockchain data in a distributed manner.
* Redis (Remote Dictionary Server) - In-memory data store, used by Spark cluster for retrieving Ethereum contracts ABIs (Application Binary Interfaces), token names, token symbols and for caching repeatable operations.
* Etherscan API - Used for transaction decoding support (only if the given information is not yet present in the Redis cache).
* Moralis API - Used for retrieving token names, symbols and other details. API responses are cached in Redis.

## Airflow graph
![airflow-graph.png](docs/airflow-graph.png)