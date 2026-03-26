FROM apache/airflow:2.9.3-python3.11

USER airflow

RUN set -eux; \
    for attempt in 1 2 3 4 5; do \
      pip install --no-cache-dir apache-airflow-providers-docker==3.11.0 && break; \
      if [ "$attempt" -eq 5 ]; then \
        echo "pip install failed after ${attempt} attempts" >&2; \
        exit 1; \
      fi; \
      echo "pip install failed on attempt ${attempt}, retrying..." >&2; \
      sleep $((attempt * 5)); \
    done
