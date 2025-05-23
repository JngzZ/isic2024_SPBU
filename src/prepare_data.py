# %%
import os
import gc
import cv2
import math
import copy
import time
import random
import glob
from matplotlib import pyplot as plt

# For data manipulation
import numpy as np
import pandas as pd

# Utils
import joblib
from tqdm import tqdm
from collections import defaultdict

# Sklearn Imports
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import StratifiedKFold, StratifiedGroupKFold, GroupKFold

# For Image Models
import timm

# Albumentations for augmentations
import albumentations as A
from albumentations.pytorch import ToTensorV2

# For colored terminal text
from colorama import Fore, Back, Style

b_ = Fore.BLUE
sr_ = Style.RESET_ALL

import warnings

warnings.filterwarnings("ignore")

# For descriptive error messages
os.environ["CUDA_LAUNCH_BLOCKING"] = "1"

# %%
ROOT_DIR = "/workspace/src/data/isic-2024-challenge"
TRAIN_DIR = f"{ROOT_DIR}/train-image/image"


def get_train_file_path(image_id):
    return f"{TRAIN_DIR}/{image_id}.jpg"


train_images = sorted(glob.glob(f"{TRAIN_DIR}/*.jpg"))

# %%
df = pd.read_csv(f"{ROOT_DIR}/train-metadata.csv")

print("        df.shape, # of positive cases, # of patients")
print("original>", df.shape, df.target.sum(), df["patient_id"].unique().shape)

#这里作者将下面的预处理脚本中 负采样代码被注释，所以实际上加载的是原始的不平衡数据，应该取消注释

df_positive = df[df["target"] == 1].reset_index(drop=True)
df_negative = df[df["target"] == 0].reset_index(drop=True)

df = pd.concat([df_positive, df_negative.iloc[:df_positive.shape[0]*20, :]])  # positive:negative = 1:20
print("filtered>", df.shape, df.target.sum(), df["patient_id"].unique().shape)

df["file_path"] = df["isic_id"].apply(get_train_file_path)
df = df[df["file_path"].isin(train_images)].reset_index(drop=True)
df

# %%
target = df.target
patient_id = df.patient_id
#这里需要额外添加一列：
#df = df[["isic_id", "patient_id", "target"]]
df = df[["isic_id", "patient_id", "target", "iddx_full"]]  # 保留 iddx_full
df.to_parquet(os.path.join(ROOT_DIR, "df_train_preprocessed.parquet"))

# %%
n_fold = 5
sgkf = StratifiedGroupKFold(n_splits=n_fold)

for fold, (train_idx, val_idx) in enumerate(sgkf.split(df, target, patient_id)):
    df.loc[train_idx, f"StratifiedGroupKFold_{n_fold}_{fold}"] = "train"
    df.loc[val_idx, f"StratifiedGroupKFold_{n_fold}_{fold}"] = "val"

# %%
n_fold = 5
gkf = GroupKFold(n_splits=n_fold)

for fold, (train_idx, val_idx) in enumerate(gkf.split(df, target, patient_id)):
    df.loc[train_idx, f"GroupKFold_{n_fold}_{fold}"] = "train"
    df.loc[val_idx, f"GroupKFold_{n_fold}_{fold}"] = "val"


# %%
# for triple-stratificate-group-kfold
def create_stratification_column(df, group_col, target_col):
    # Group by patient and calculate features for stratification
    patient_info = df.groupby(group_col).agg(
        {
            target_col: "mean",  # Proportion of malignant images per patient
            group_col: "count",  # Number of images per patient
        }
    )

    # Rename the count column to avoid confusion
    patient_info = patient_info.rename(columns={group_col: "image_count"})

    # Create bins for number of images per patient
    #patient_info["image_count_bin"] = pd.qcut(patient_info["image_count"], q=10, labels=False)  #这里会生成重复的边界 需要修改

    #修改：
    patient_info["image_count_bin"] = pd.qcut(
        patient_info["image_count"],
        q=10,
        labels=False,
        duplicates='drop'  # 允许丢弃重复的边界
    )

    # Combine stratification features
    patient_info["strat"] = (
        (patient_info[target_col] * 10).astype(int).astype(str)
        + "_"
        + patient_info["image_count_bin"].astype(str)
    )

    # Merge back to original dataframe
    df = df.join(patient_info["strat"], on=group_col)

    return df["strat"]


# data loading
df["strat"] = create_stratification_column(df, "patient_id", "target")

groups = df["patient_id"]
strat = df["strat"]

cv = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=0)

# for fold, (train_idx, val_idx) in enumerate(cv.split(df, target, patient_id)):
#     df.loc[train_idx, f"TSGKF_{n_fold}_{fold}"] = "train"
#     df.loc[val_idx, f"TSGKF_{n_fold}_{fold}"] = "val"
# # %%
# df = df.drop(["patient_id", "target", "strat"], axis=1)
# df.to_parquet(os.path.join(ROOT_DIR, "df_train_preprocessed.parquet"))


# 保存每个 fold 的划分结果
for fold in range(n_fold):
    max_attempts = 100  # 最大尝试次数，防止无限循环
    has_positive = False

    # 多次尝试生成包含正样本的验证集
    for _ in range(max_attempts):
        # 生成划分
        train_idx, val_idx = next(cv.split(df, df["target"], df["patient_id"]))

        # 检查验证集是否有正样本
        if np.sum(df.iloc[val_idx]["target"]) >= 1:
            has_positive = True
            break

    if not has_positive:
        raise ValueError(f"Fold {fold} 无法生成包含正样本的验证集，请检查数据分布！")

    # 标记训练集和验证集
    df.loc[train_idx, f"TSGKF_{n_fold}_{fold}"] = "train"
    df.loc[val_idx, f"TSGKF_{n_fold}_{fold}"] = "val"
    print(f"Fold {fold}: 验证集正样本数 = {np.sum(df.iloc[val_idx]['target'])}")

# 删除无用列并保存
df = df.drop(["patient_id", "target", "strat"], axis=1)
df.to_parquet(os.path.join(ROOT_DIR, "df_train_preprocessed.parquet"))


# %%

# %% iddxクラスタリングによるクラス作成
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
from sklearn.metrics import pairwise_distances

# # データフレームの読み込み
# df = pd.read_csv("/workspace/src/data/isic-2024-challenge/train-metadata.csv")
#
# # 正确：加载预处理后的数据（8253条）
# #df = pd.read_parquet(os.path.join(ROOT_DIR, "df_train_preprocessed.parquet"))
#
# df["iddx_only_benign"] = df["iddx_full"] == "Benign"
#
# df_limit_benign = pd.concat(
#     [df[df["iddx_only_benign"] == False], df[df["iddx_only_benign"] == True].sample(500)]
# )
#
# # %%
# # TF-IDFベクトライザーの使用
# vectorizer = TfidfVectorizer()
# X = vectorizer.fit_transform(df_limit_benign["iddx_full"])
#
# # K-meansクラスタリングの適用
# n_clusters = 7  # クラスターの数を決定
# kmeans = KMeans(n_clusters=n_clusters, random_state=0).fit(X)
#
# df[f"iddx_cluster_{n_clusters}"] = np.nan
# df.loc[df["iddx_only_benign"] == False, f"iddx_cluster_{n_clusters}"] = kmeans.labels_[:-500]
# # 'Benign'は個別のクラスターとする
# df.loc[df["iddx_only_benign"] == True, f"iddx_cluster_{n_clusters}"] = kmeans.labels_[-1]
#
# # クラスタリング結果の確認
# print(df[f"iddx_cluster_{n_clusters}"].value_counts())


#上面的代码需要大改，所以先注释掉，下面的代码确保使用了处理后的数据进行加载聚类
# 加载预处理后的数据（8253 条）
df = pd.read_parquet(os.path.join(ROOT_DIR, "df_train_preprocessed.parquet"))

# 仅对非良性样本和500个良性样本聚类
df["iddx_only_benign"] = df["iddx_full"] == "Benign"
df_limit_benign = pd.concat([
    df[df["iddx_only_benign"] == False],
    df[df["iddx_only_benign"] == True].sample(500, random_state=42)
])

# 执行聚类（后续代码不变）
vectorizer = TfidfVectorizer()
X = vectorizer.fit_transform(df_limit_benign["iddx_full"])

# K-meansクラスタリングの適用
n_clusters = 7  # クラスターの数を決定
kmeans = KMeans(n_clusters=n_clusters, random_state=0).fit(X)
#kmeans = KMeans(n_clusters=7, random_state=0).fit(X)

# 合并结果到 DataFrame
df["iddx_cluster_7"] = np.nan
df.loc[df["iddx_only_benign"] == False, "iddx_cluster_7"] = kmeans.labels_[:-500]
df.loc[df["iddx_only_benign"] == True, "iddx_cluster_7"] = kmeans.labels_[-1]

# 打印验证
print("iddx_cluster_7 分布（过滤后数据）:")
print(df["iddx_cluster_7"].value_counts())


# %% t-SNE
from sklearn.manifold import TSNE

# t-SNEの適用
tsne = TSNE(n_components=2, random_state=42)
X_embedded = tsne.fit_transform(X.toarray())

# %%
# 結果のプロット
plt.figure(figsize=(10, 8))
scatter = plt.scatter(X_embedded[:, 0], X_embedded[:, 1], c=kmeans.labels_, cmap="Accent", s=5)
plt.colorbar(scatter)
plt.title("t-SNE Visualization of Digits Dataset")
plt.xlabel("t-SNE Component 1")
plt.ylabel("t-SNE Component 2")
plt.show()


# %% クラスタごとの最頻値を確認
def generate_cluster_labels(df, n_clusters):
    cluster_labels = {}
    for i in range(n_clusters):
        cluster_data = df[df[f"iddx_cluster_{n_clusters}"] == i]
        iddx_mode = cluster_data["iddx_full"].mode()[0]
        cluster_labels[i] = iddx_mode
    return cluster_labels


cluster_labels = generate_cluster_labels(df, n_clusters)
cluster_labels

# %%
# df[["isic_id", f"iddx_cluster_{n_clusters}"]].to_parquet(
#     f"/workspace/data/isic-2024-challenge/df_train_iddx_cluster_{n_clusters}.parquet"
# )

# %%
import torch

# クラスターの中心
cluster_centers = kmeans.cluster_centers_

# 各データポイントとそのクラスター中心との距離を計算
distances = pairwise_distances(X, cluster_centers, metric="euclidean")

labels = torch.softmax(-torch.from_numpy(distances) * 5, 1).numpy()

label_cols = []
for n in range(n_clusters):
    df[f"iddx_cluster_{n_clusters}_label_{n}"] = np.nan
    df.loc[df["iddx_only_benign"] == False, f"iddx_cluster_{n_clusters}_label_{n}"] = labels[:-500][:, n]
    # 'Benign'は個別のクラスターとする
    df.loc[df["iddx_only_benign"] == True, f"iddx_cluster_{n_clusters}_label_{n}"] = labels[-1][n]
    label_cols.append(f"iddx_cluster_{n_clusters}_label_{n}")

df[["isic_id"] + label_cols].to_parquet(
    f"/workspace/src/data/isic-2024-challenge/df_train_iddx_cluster_{n_clusters}_temp5_label.parquet"
)
