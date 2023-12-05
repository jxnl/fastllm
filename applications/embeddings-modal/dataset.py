import requests
import datasets
import time
import subprocess
import pandas as pd
import json
from typing import List

# Replace with your equivalent Modal Labs endpoint
MODAL_ENDPOINT = "https://567-labs--embedder-fastapi-app-jumshim-dev.modal.run/embed"
DATASET = {
  "name": "wikipedia",
  "subset": "20220301.simple",
}
EXCEL_FILE = "embeddings.xlsx"
FEATURE_FLAG = "BATCH"
BATCH_SIZE = 20


# Wrapper Benchmarker to evaluate runtime of each function
def benchmark(func):
  def wrapper(*args, **kwargs):
    start_time = time.time()
    result = func(*args, **kwargs)
    end_time = time.time()
    print(f"Time taken for {func.__name__}: {end_time - start_time} seconds")
    return result
  return wrapper

@benchmark
def start_modal_server():
  subprocess.run(["modal", "serve", "modal_main.py"])

# Benchmarking for Batch Requests
def get_embeddings_batch(texts: List[str]) -> List[List[float]]:
  response = requests.post(
    MODAL_ENDPOINT,
    json={"input": texts},
  )
  response.raise_for_status()
  return [item["embedding"] for item in response.json()["data"]]

def process_dataset_batch(dataset, batch_size=10):
  embeddings = []
  batch_texts = []
  for i, item in enumerate(dataset):
    batch_texts.append(item['text'])
    if len(batch_texts) == batch_size:
      batch_embeddings = get_embeddings_batch(batch_texts)
      embeddings.extend(batch_embeddings)
      batch_texts = []

# Benchmarking for Linear Requests
@benchmark
def get_embeddings_linear(texts: List[str]) -> List[List[float]]:
  response = requests.post(
    MODAL_ENDPOINT,
    json={"input": texts}
  )
  response.raise_for_status()
  return [item["embedding"] for item in response.json()["data"]]

def process_dataset_linear(dataset):
  embeddings = []
  for i, item in enumerate(dataset):
    if (i >= 100):
      break
    embedding = get_embeddings_linear([item['text']])
    embeddings.append(embedding)
  return embeddings

def save_to_excel(data):
  df = pd.dataframe(data)
  df.to_excel(EXCEL_FILE, index=False)
  print(f"Data saved to: {EXCEL_FILE}")

def main():
  dataset = datasets.load_dataset(DATASET["name"], DATASET["subset"])['train']
  start = time.time()
  if FEATURE_FLAG == "BATCH":
    embeddings = process_dataset_batch(dataset, BATCH_SIZE)
  elif FEATURE_FLAG == "LINEAR":
    embeddings = process_dataset_linear(dataset)
  else:
    raise ValueError("Invalid feature flag. Choose 'BATCH' or 'LINEAR'")
  end = time.time()
  save_to_excel(embeddings)

  print(f"--------TOTAL TIME TO PROCESS-------- \n {end-start}s")

if __name__ == "__main__":
  main()

