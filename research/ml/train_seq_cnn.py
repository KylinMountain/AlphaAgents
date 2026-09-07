"""1D CNN 序列模型 — 4 个 binary classifier 多任务训练.

Architecture:
  Input:  (B, 60, 5)  — 60 天 × OHLCV
  Conv1D blocks: 32 → 64 → 128 channels
  GlobalAvgPool → FC → 4 binary outputs

Train on Apple Silicon MPS GPU.
"""
import argparse, pickle, time
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
from sklearn.metrics import roc_auc_score


SEQ_LEN = 60
N_CHANNELS = 5  # OHLCV


class VPAConv1D(nn.Module):
    def __init__(self, n_channels=5, n_classes=4):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv1d(n_channels, 32, kernel_size=5, padding=2),
            nn.BatchNorm1d(32), nn.ReLU(),
            nn.Conv1d(32, 64, kernel_size=5, padding=2),
            nn.BatchNorm1d(64), nn.ReLU(),
            nn.MaxPool1d(2),
            nn.Conv1d(64, 128, kernel_size=3, padding=1),
            nn.BatchNorm1d(128), nn.ReLU(),
            nn.Conv1d(128, 128, kernel_size=3, padding=1),
            nn.BatchNorm1d(128), nn.ReLU(),
            nn.AdaptiveAvgPool1d(1),
        )
        self.head = nn.Sequential(
            nn.Linear(128, 64), nn.ReLU(), nn.Dropout(0.3),
            nn.Linear(64, n_classes),
        )

    def forward(self, x):
        # x: (B, 60, 5) → (B, 5, 60)
        x = x.transpose(1, 2)
        z = self.conv(x).squeeze(-1)
        return self.head(z)  # (B, 4) logits


def regime_split(meta):
    """Return mask arrays — 标准时序 split (per user request).
    Train: 2024-04 → 2025-09 (18 months)
    Val:   2025-10 → 2025-12 (3 months, early stopping)
    Test:  2026-01 → 2026-04 (4 months, 含 大涨/大跌/反弹 3 regime)
    """
    asof = np.array([m['as_of'] for m in meta])
    train = (asof >= '2024-04-01') & (asof <= '2025-09-30')
    val = (asof >= '2025-10-01') & (asof <= '2025-12-31')
    test = (asof >= '2026-01-01') & (asof <= '2026-04-30')
    # Sub-regime in test for analysis
    test_jan = (asof >= '2026-01-01') & (asof <= '2026-01-31')
    test_feb_mar = (asof >= '2026-02-01') & (asof <= '2026-03-31')
    test_apr = (asof >= '2026-04-01') & (asof <= '2026-04-30')
    return train, val, test, test_jan, test_feb_mar, test_apr


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--data', default='/Users/evilkylin/Projects/AlphaAgents/data/vpa_seq_dataset.npz')
    p.add_argument('--epochs', type=int, default=20)
    p.add_argument('--batch', type=int, default=512)
    p.add_argument('--lr', type=float, default=3e-4)
    p.add_argument('--out-model', default='/Users/evilkylin/Projects/AlphaAgents/data/vpa_seq_cnn.pt')
    args = p.parse_args()

    device = torch.device('mps' if torch.backends.mps.is_available() else 'cpu')
    print(f'Device: {device}', flush=True)

    print('Loading dataset...', flush=True)
    data = np.load(args.data)
    X = data['X']
    Y = np.column_stack([data['y_topping'], data['y_bottoming'],
                          data['y_continuation'], data['y_breakdown']]).astype(np.float32)
    with open(args.data.replace('.npz', '_meta.pkl'), 'rb') as f:
        meta = pickle.load(f)
    print(f'  X={X.shape}, Y={Y.shape}', flush=True)

    train_mask, val_mask, test_mask, test_jan, test_feb_mar, test_apr = regime_split(meta)
    print(f'  Train: {train_mask.sum()}  Val: {val_mask.sum()}  Test (all): {test_mask.sum()}')
    print(f'    Test Jan (大涨): {test_jan.sum()}, Feb-Mar (大跌): {test_feb_mar.sum()}, Apr (反弹): {test_apr.sum()}')

    # Tensors
    X_tr = torch.tensor(X[train_mask]); Y_tr = torch.tensor(Y[train_mask])
    X_val = torch.tensor(X[val_mask]); Y_val = torch.tensor(Y[val_mask])
    X_te = torch.tensor(X[test_mask]); Y_te = torch.tensor(Y[test_mask])
    X_t1 = torch.tensor(X[test_jan]); Y_t1 = torch.tensor(Y[test_jan])
    X_tfm = torch.tensor(X[test_feb_mar]); Y_tfm = torch.tensor(Y[test_feb_mar])
    X_t2 = torch.tensor(X[test_apr]); Y_t2 = torch.tensor(Y[test_apr])

    train_loader = DataLoader(TensorDataset(X_tr, Y_tr), batch_size=args.batch, shuffle=True)
    val_loader = DataLoader(TensorDataset(X_val, Y_val), batch_size=args.batch)

    model = VPAConv1D(n_channels=N_CHANNELS, n_classes=4).to(device)
    print(f'\nModel: {sum(p.numel() for p in model.parameters()):,} params')

    # Class-weighted BCE for imbalance
    pos_weights = []
    for i in range(4):
        rate = Y_tr[:, i].mean().item()
        pos_weights.append((1-rate)/max(rate, 0.01))
    pos_w = torch.tensor(pos_weights, dtype=torch.float32, device=device)
    print(f'  pos_weights: {pos_weights}')

    optim = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    crit = nn.BCEWithLogitsLoss(pos_weight=pos_w)

    best_val_auc = 0
    best_epoch = 0
    label_names = ['topping','bottoming','continuation','breakdown']

    for epoch in range(args.epochs):
        t0 = time.time()
        model.train()
        loss_sum = 0; n = 0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            logits = model(xb)
            loss = crit(logits, yb)
            optim.zero_grad(); loss.backward(); optim.step()
            loss_sum += loss.item() * len(xb); n += len(xb)
        train_loss = loss_sum / n

        # Eval
        model.eval()
        with torch.no_grad():
            def eval_set(X_, Y_):
                p = torch.sigmoid(model(X_.to(device))).cpu().numpy()
                aucs = []
                for i in range(4):
                    y = Y_[:, i].numpy()
                    if len(set(y)) > 1:
                        aucs.append(roc_auc_score(y, p[:, i]))
                    else:
                        aucs.append(0.5)
                return aucs, p
            val_aucs, val_p = eval_set(X_val, Y_val)
            mean_val = np.mean(val_aucs)

        elapsed = time.time() - t0
        marker = '⭐' if mean_val > best_val_auc else ' '
        print(f'  Epoch {epoch+1:>2}/{args.epochs}  train_loss={train_loss:.4f}  '
              f'val AUC: top={val_aucs[0]:.3f} bot={val_aucs[1]:.3f} '
              f'cont={val_aucs[2]:.3f} brk={val_aucs[3]:.3f}  mean={mean_val:.3f}  '
              f'{elapsed:.0f}s {marker}')

        if mean_val > best_val_auc:
            best_val_auc = mean_val; best_epoch = epoch + 1
            torch.save(model.state_dict(), args.out_model)

    # Test eval with best model
    print(f'\nLoading best model from epoch {best_epoch} (val mean AUC {best_val_auc:.3f})')
    model.load_state_dict(torch.load(args.out_model))
    model.eval()
    with torch.no_grad():
        for set_name, X_, Y_ in [
            ('Train', X_tr, Y_tr),
            ('Val', X_val, Y_val),
            ('Test (all)', X_te, Y_te),
            ('  Test Jan (大涨)', X_t1, Y_t1),
            ('  Test Feb-Mar (大跌)', X_tfm, Y_tfm),
            ('  Test Apr (反弹)', X_t2, Y_t2),
        ]:
            # Process in batches to avoid OOM
            ps = []
            for i in range(0, len(X_), args.batch):
                xb = X_[i:i+args.batch].to(device)
                ps.append(torch.sigmoid(model(xb)).cpu().numpy())
            p = np.concatenate(ps)
            aucs = []
            for i in range(4):
                y = Y_[:, i].numpy()
                aucs.append(roc_auc_score(y, p[:, i]) if len(set(y)) > 1 else 0.5)
            print(f'  {set_name:<14} N={len(Y_):>6}  AUC: top={aucs[0]:.3f} bot={aucs[1]:.3f} '
                  f'cont={aucs[2]:.3f} brk={aucs[3]:.3f}  mean={np.mean(aucs):.3f}')


if __name__ == '__main__':
    main()
