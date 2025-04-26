import h5py
import pandas as pd

# 检查 HDF5 文件中的图像数量
with h5py.File("/workspace/src/data/isic-2024-challenge/train-image.hdf5", "r") as f:
    print(f"HDF5 训练集图像数量: {len(f['images'])}")

# 检查 CSV 文件的样本数量
df_meta = pd.read_csv("/workspace/src/data/isic-2024-challenge/train-metadata.csv")
print(f"CSV 训练集样本数量: {len(df_meta)}")