import itertools
from typing import List
from modal import Image, Stub, Volume, Secret, gpu
import os
from sklearn.metrics import accuracy_score, precision_score, recall_score, roc_auc_score
from evals import threshold_decorator
from datasets import Dataset

# Model Configuration
MODELS = [
    "BAAI/bge-base-en-v1.5",
    "llmrails/ember-v1",
    "thenlper/gte-large",
    "infgrad/stella-base-en-v2",
    "sentence-transformers/gtr-t5-large",
]
GPU_CONFIG = gpu.A100()

# Eval COnfiguration
METRICS = {
    "accuracy": accuracy_score,  # This is the number of correct predictions by the model ( TP + TN )/ (# of samples)
    "precision": precision_score,  # This measures the number of positive class predicitons (TP) / (TP + FP)
    "recall": recall_score,  # This measure the number of negative class predictions (TP) / ( TP + FN )
}
THRESHOLDS = [0.3, 0.5, 0.7]
EVALS = dict()
for threshold, metric_name in itertools.product(THRESHOLDS, METRICS.keys()):
    EVALS[f"{metric_name} ({threshold})"] = threshold_decorator(threshold)(
        METRICS[metric_name]
    )
EVALS["AUC"] = roc_auc_score

# Dataset Configuration
DATASET_NAME = "567-labs/cleaned-quora-dataset-train-test-split"
DATASET_DIR = "/data"
DATASET_VOLUME = Volume.persisted("datasets")

# Test Configuration
TEST_PERCENTAGE = 0.1
MAXIMUM_ELEMENTS_TO_TEST = 300
COHERE_MODEL = "embed-english-v3.0"

stub = Stub("cohere-embeddings")


image = Image.debian_slim().pip_install(
    "cohere", "datasets", "sentence-transformers", "scikit-learn", "tabulate", "openai"
)


def get_unique_sentences(
    test_dataset: Dataset, sentence_to_id_mapping: dict, batch_size=1000
):
    seen = set()
    batch = []
    for row in test_dataset:
        s1, s2 = row["questions"]["text"]

        if s1 not in seen:
            sentence_to_id_mapping[s1] = len(seen)
            seen.add(s1)
            batch.append(s1)

            if len(batch) == batch_size:
                yield batch
                batch = []

        if s2 not in seen:
            sentence_to_id_mapping[s2] = len(seen)
            seen.add(s2)
            batch.append(s2)

            if len(batch) == batch_size:
                yield batch
                batch = []

    if batch:
        yield batch


def extract_unique_sentences(test_dataset: Dataset, sentence_to_id_mapping: dict):
    sentence_pair_elements_1 = []
    sentence_pair_elements_2 = []
    labels = []

    for row in test_dataset:
        s1, s2 = row["questions"]["text"]
        sentence_pair_elements_1.append(sentence_to_id_mapping[s1])
        sentence_pair_elements_2.append(sentence_to_id_mapping[s2])
        labels.append(1 if row["is_duplicate"] else 0)

    return sentence_pair_elements_1, sentence_pair_elements_2, labels


@stub.function(image=image, volumes={DATASET_DIR: DATASET_VOLUME})
def generate_dataset_split():
    from datasets import load_dataset, load_from_disk

    dataset_path = f"{DATASET_DIR}/{DATASET_NAME}"

    if os.path.exists(dataset_path):
        dataset = load_from_disk(f"{DATASET_DIR}/{DATASET_NAME}")
        return

    dataset = load_dataset(DATASET_NAME)

    dataset.save_to_disk(dataset_path)
    DATASET_VOLUME.commit()


def generate_quora_input_example(examples):
    from sentence_transformers import InputExample

    return [
        InputExample(
            texts=[
                example["questions"]["text"][0],
                example["questions"]["text"][1],
            ],
            label=int(example["is_duplicate"]),
        )
        for example in examples
    ]


@stub.function(
    image=image, volumes={DATASET_DIR: DATASET_VOLUME}, gpu=GPU_CONFIG, timeout=1200
)
async def benchmark_mteb_model(model_name):
    from datasets import load_from_disk
    from sentence_transformers import util, SentenceTransformer
    import numpy as np
    import torch.nn as nn
    import torch

    dataset = load_from_disk(f"{DATASET_DIR}/{DATASET_NAME}")
    test_dataset = dataset["test"]
    model = SentenceTransformer(model_name)

    sentence_to_embedding_map = dict()
    batch_size = 5000
    sentences = get_unique_sentences(
        test_dataset, sentence_to_embedding_map, batch_size
    )
    embeddings = []
    for item in sentences:
        embeddings.extend(model.encode(item))

    embeddings_tensor = torch.tensor(np.array(embeddings)).to("cuda")
    # Create an embedding layer with pre-trained weights
    embedding_layer = nn.Embedding.from_pretrained(embeddings_tensor)

    sentence_pairs_elem_1, sentence_pairs_elem_2, labels = extract_unique_sentences(
        test_dataset, sentence_to_embedding_map
    )
    sentence_pairs_embedding_1 = embedding_layer(
        torch.as_tensor(sentence_pairs_elem_1, device="cuda")
    )
    sentence_pairs_embedding_2 = embedding_layer(
        torch.as_tensor(sentence_pairs_elem_2, device="cuda")
    )

    cosine_scores = util.cos_sim(sentence_pairs_embedding_1, sentence_pairs_embedding_2)
    predictions = np.diag(cosine_scores.cpu()).tolist()
    return {name: f(labels, predictions) for name, f in EVALS.items()}


@stub.function(
    image=image,
    volumes={DATASET_DIR: DATASET_VOLUME},
    secret=Secret.from_name("openai"),
    timeout=86400,
    gpu=GPU_CONFIG,
)
async def benchmark_openai():
    from datasets import load_from_disk
    from sentence_transformers import util
    from sklearn.metrics import roc_auc_score
    import asyncio
    import time
    from tqdm import tqdm
    from openai import AsyncOpenAI
    import numpy as np
    import torch
    import torch.nn as nn

    dataset = load_from_disk(f"{DATASET_DIR}/{DATASET_NAME}")
    test_dataset = dataset["test"]
    client = AsyncOpenAI()

    sentence_to_embedding_map = dict()
    batch_size = 128
    num_sentences = 86235  # Number of unique sentences ( derived from len(sentences))

    sem = asyncio.Semaphore(20)
    sentences = get_unique_sentences(
        test_dataset, sentence_to_embedding_map, batch_size
    )
    tqdm_monitoring_bar = tqdm(total=num_sentences)

    async def embed_text(texts, progress_bar: tqdm):
        async with sem:
            response = await client.embeddings.create(
                input=texts, model="text-embedding-ada-002"
            )
            progress_bar.update(len(texts))
            assert len(response.data) == len(
                texts
            ), f"Response was {len(response)} when {len(texts)} entities were passed in"

            res = []
            for item in response.data:
                assert len(item.embedding) == 1536
                res.append(item.embedding)
            return res

    start = time.time()
    coros = [
        embed_text(sentence_group, tqdm_monitoring_bar) for sentence_group in sentences
    ]
    res = await asyncio.gather(*coros)
    tqdm_monitoring_bar.close()
    end = time.time()

    flattened_res = [item for sublist in res for item in sublist]
    print(f"Processed {len(flattened_res)} embeddings in {end-start}s")
    embeddings_tensor = torch.tensor(flattened_res).to("cuda")
    embedding_layer = nn.Embedding.from_pretrained(embeddings_tensor)

    sentence_pairs_elem_1, sentence_pairs_elem_2, labels = extract_unique_sentences(
        test_dataset, sentence_to_embedding_map
    )
    sentence_pairs_embedding_1 = embedding_layer(
        torch.as_tensor(sentence_pairs_elem_1, device="cuda")
    )
    sentence_pairs_embedding_2 = embedding_layer(
        torch.as_tensor(sentence_pairs_elem_2, device="cuda")
    )

    cosine_scores = util.cos_sim(sentence_pairs_embedding_1, sentence_pairs_embedding_2)
    predictions = np.diag(cosine_scores.cpu()).tolist()
    return {name: f(labels, predictions) for name, f in EVALS.items()}


@stub.function(
    image=image,
    volumes={DATASET_DIR: DATASET_VOLUME},
    secret=Secret.from_name("cohere"),
    timeout=86400,
    gpu=GPU_CONFIG,
)
async def benchmark_cohere_roc():
    from cohere import Client
    from datasets import load_from_disk
    from sentence_transformers import util
    from sklearn.metrics import roc_auc_score
    import asyncio
    import time
    from tqdm import tqdm
    import numpy as np
    import torch
    import torch.nn as nn

    dataset = load_from_disk(f"{DATASET_DIR}/{DATASET_NAME}")
    test_dataset = dataset["test"]
    co = Client(os.environ["COHERE_API_KEY"])
    sem = asyncio.Semaphore(64)
    num_sentences = 86235  # len(test_dataset)
    sentence_to_embedding_map = dict()
    batch_size = 96
    sentences = get_unique_sentences(
        test_dataset, sentence_to_embedding_map, batch_size
    )
    tqdm_monitoring_bar = tqdm(total=num_sentences)

    async def embed_text(texts, progress_bar: tqdm):
        async with sem:
            response = co.embed(
                texts=texts,
                model="embed-multilingual-v3.0",
                input_type="clustering",
            )
            progress_bar.update(len(texts))
            assert len(response.embeddings) == len(texts)

            for item in response.embeddings:
                assert len(item) == 1024
            return response.embeddings

    start = time.time()
    coros = [
        embed_text(sentence_group, tqdm_monitoring_bar) for sentence_group in sentences
    ]
    res = await asyncio.gather(*coros)
    end = time.time()
    tqdm_monitoring_bar.close()
    print(f"Generated embeddings in {end-start}s")

    # Flatten the list of lists and convert to tensor of floats
    flattened_res = [item for sublist in res for item in sublist]
    print(f"Processed {len(flattened_res)} embeddings in {end-start}s")
    embeddings_tensor = torch.tensor(flattened_res).to("cuda")
    embedding_layer = nn.Embedding.from_pretrained(embeddings_tensor)

    sentence_pairs_elem_1, sentence_pairs_elem_2, labels = extract_unique_sentences(
        test_dataset, sentence_to_embedding_map
    )
    sentence_pairs_embedding_1 = embedding_layer(
        torch.as_tensor(sentence_pairs_elem_1, device="cuda")
    )
    sentence_pairs_embedding_2 = embedding_layer(
        torch.as_tensor(sentence_pairs_elem_2, device="cuda")
    )

    cosine_scores = util.cos_sim(sentence_pairs_embedding_1, sentence_pairs_embedding_2)
    predictions = np.diag(cosine_scores.cpu()).tolist()
    return {name: f(labels, predictions) for name, f in EVALS.items()}


@stub.local_entrypoint()
def main():
    from tabulate import tabulate
    import re
    import json

    def sort_key(key):
        match = re.search(r"\((\d+(\.\d+)?)\)", key)
        if match:
            return (True, float(match.group(1)))
        else:
            return (False, key)

    # generate_dataset_split.remote()

    res = {}
    res["text-embeddings-ada-v2"] = benchmark_openai.remote()
    res["embed-multilingual-v3.0"] = benchmark_cohere_roc.remote()

    for model_name, evals in zip(
        MODELS, benchmark_mteb_model.map(MODELS, order_outputs=True)
    ):
        res[model_name] = evals

    with open("results.json", "w") as f:
        json.dump(res, f)
    keys = list(EVALS.keys())
    keys.sort(key=sort_key)

    values = [
        [model, *[eval_perf[eval_name] for eval_name in keys]]
        for model, eval_perf in res.items()
    ]

    print(tabulate(values, ["Model Name", *keys]))