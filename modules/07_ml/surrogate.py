"""
modules/07_ml/surrogate.py - the instant what-if emulator.

The fast solver takes 20-40 seconds for a 40 km reach. That is fine for
"press run and wait", and far too slow for "drag the reservoir-level slider and
watch the flood respond". This module learns the solver's input-to-output map
once, offline, and then answers in milliseconds on the GPU.

    python -m modules.07_ml.surrogate dataset --n 60     # run the solver N times
    python -m modules.07_ml.surrogate train --epochs 120
    python -m modules.07_ml.surrogate check

WHAT THIS IS, precisely, because the distinction decides whether we can defend
it: the network is trained on OUR OWN SOLVER'S OUTPUT. It is an emulator of the
shallow-water model, not a model of reality. Its error is measured against the
solver, and quoting it as flood-prediction accuracy would be false. What it
buys is interactivity - and the honest framing in front of a juror is "the
physics runs in 30 seconds; this reproduces the physics in 4 milliseconds so
the operator can explore scenarios, and every headline result on screen is
recomputed with the real solver before it is exported."

Architecture: a small U-Net. The mapping from (terrain, breach parameters) to
(max depth, arrival time) is strongly local - water goes downhill from where it
is released - and a U-Net's skip connections preserve exactly the fine channel
detail that a plain encoder-decoder blurs away. It fits in 4 GB with room to
spare.

Owner: captain (module 07).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "surrogate"
MODEL_DIR = REPO_ROOT / "modules" / "07_ml" / "models"
MODEL_PATH = MODEL_DIR / "surrogate_unet.pt"

# The scenario knobs the surrogate learns to respond to. Each becomes an input
# channel, broadcast across the grid and normalised to roughly 0..1.
PARAMS = ("reservoir_level_frac", "capacity_mcm", "dam_height_m", "formation_time_hr")
PARAM_SCALE = {
    "reservoir_level_frac": 1.0,
    "capacity_mcm": 100.0,
    "dam_height_m": 200.0,
    "formation_time_hr": 3.0,
}

# Fixed training site. The emulator is per-site by design: terrain is the one
# input that does not vary within a deployment, and holding it fixed means the
# network spends its capacity on the scenario response instead of relearning
# geography it will only ever see once.
SITE = "teesta"
SITE_LATLON = (27.6003, 88.6428)
REACH_KM = 30.0
CORRIDOR_KM = 10.0
CELLSIZE_M = 150.0
END_HR = 8.0


# ==========================================================================
# Dataset
# ==========================================================================


def _pad_to_multiple(a: np.ndarray, k: int = 8) -> np.ndarray:
    """Pad an array up to a multiple of k in both axes.

    A U-Net halves the resolution at each level, so a shape that is not
    divisible by 2^levels cannot be reassembled by the decoder.
    """
    ny, nx = a.shape[-2:]
    py, px = (-ny) % k, (-nx) % k
    if py == 0 and px == 0:
        return a
    pad = [(0, 0)] * (a.ndim - 2) + [(0, py), (0, px)]
    return np.pad(a, pad, mode="edge")


def build_dataset(n: int = 60, seed: int = 26161, out_dir: Path = DATA_DIR) -> dict:
    """Run the solver n times with sampled parameters and store the results.

    This is the expensive step - it is literally n full simulations - and it is
    the reason the surrogate is honest: every training target is a real solver
    output on real terrain, not a synthetic pattern.

    Sampling is Latin-hypercube-like (a shuffled stratified grid per parameter)
    rather than uniform random, so 60 samples cover the corners of the parameter
    space instead of clustering in the middle.
    """
    import importlib

    sc = importlib.import_module("modules.04_backend.scenario")
    rn = importlib.import_module("modules.04_backend.runner")
    gd = importlib.import_module("modules.01_geodata")

    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    plan = gd.plan_domain(
        lat=SITE_LATLON[0], lon=SITE_LATLON[1], site=SITE,
        reach_length_km=REACH_KM, corridor_width_km=CORRIDOR_KM,
    )
    terr = gd.RealTerrain(
        site=SITE, source="COP30", dam_lonlat=plan.dam_lonlat, reach_length_km=REACH_KM
    )
    dem, manning, grid = terr.get_terrain(plan.bbox, CELLSIZE_M)

    def strat(lo, hi):
        """Stratified sample: one draw from each of n equal bins, shuffled."""
        edges = np.linspace(lo, hi, n + 1)
        pts = edges[:-1] + rng.random(n) * np.diff(edges)
        rng.shuffle(pts)
        return pts

    levels = strat(0.55, 1.0)
    caps = strat(1.0, 60.0)
    heights = strat(25.0, 120.0)

    samples, metas = [], []
    t0 = time.perf_counter()

    for i in range(n):
        site = sc.SiteSpec(
            name="Surrogate Training Site", river="Teesta", state="Sikkim",
            lat=plan.dam_lonlat[1], lon=plan.dam_lonlat[0],
            dam_height_m=float(heights[i]), reservoir_capacity_mcm=float(caps[i]),
        )
        spec = sc.ScenarioSpec(
            site=site, reservoir_level_frac=float(levels[i]),
            reach_length_km=REACH_KM, corridor_width_km=CORRIDOR_KM,
            cellsize_m=CELLSIZE_M, end_hr=END_HR, dem_source="COP30",
            domain_bbox=plan.bbox,
        )
        try:
            rd = rn.run_scenario(
                spec, outputs_dir=out_dir / "runs",
                run_id=f"surrogate_train_fast_{i:03d}", terrain=terr, write_png=False,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  sample {i}: FAILED {type(exc).__name__}: {exc}")
            continue

        from shared.io import read_grid, read_meta

        depth, _ = read_grid(rd, "max_depth")
        arrival, _ = read_grid(rd, "arrival_time")
        meta = read_meta(rd)
        breach = meta["scenario"]

        samples.append(
            {
                "depth": depth.astype(np.float32),
                "arrival": np.nan_to_num(arrival, nan=END_HR).astype(np.float32),
                "params": np.array(
                    [
                        levels[i],
                        caps[i],
                        heights[i],
                        float(breach.get("formation_time_hr", 0.5)),
                    ],
                    dtype=np.float32,
                ),
            }
        )
        metas.append(
            {
                "i": i,
                "reservoir_level_frac": float(levels[i]),
                "capacity_mcm": float(caps[i]),
                "dam_height_m": float(heights[i]),
                "peak_cumecs": meta["results"]["peak_discharge_cumecs"],
                "flood_area_km2": meta["results"]["flood_area_km2"],
                "mass_err_pct": meta["results"]["mass_balance_err_pct"],
            }
        )
        if (i + 1) % 5 == 0:
            print(f"  {i + 1}/{n} runs, {time.perf_counter() - t0:.0f}s elapsed")

    if not samples:
        raise RuntimeError("no training samples were produced")

    X_dem = _pad_to_multiple(dem.astype(np.float32))
    depths = _pad_to_multiple(np.stack([s["depth"] for s in samples]))
    arrivals = _pad_to_multiple(np.stack([s["arrival"] for s in samples]))
    params = np.stack([s["params"] for s in samples])

    np.savez_compressed(
        out_dir / "dataset.npz",
        dem=X_dem, depth=depths, arrival=arrivals, params=params,
        end_hr=END_HR, cellsize_m=grid.cellsize_m(), bbox=np.array(grid.bbox),
    )
    (out_dir / "dataset_meta.json").write_text(json.dumps(metas, indent=1))

    return {
        "samples": len(samples),
        "grid": list(depths.shape[1:]),
        "seconds": round(time.perf_counter() - t0, 1),
        "path": str(out_dir / "dataset.npz"),
    }


# ==========================================================================
# Model
# ==========================================================================


def _build_unet(in_ch: int, out_ch: int = 2, base: int = 24):
    """A three-level U-Net. Defined inside a function so importing this module
    does not require torch - the damage model and the API must keep working on
    a machine with no GPU stack installed."""
    import torch
    import torch.nn as nn

    def block(a, b):
        return nn.Sequential(
            nn.Conv2d(a, b, 3, padding=1), nn.BatchNorm2d(b), nn.ReLU(inplace=True),
            nn.Conv2d(b, b, 3, padding=1), nn.BatchNorm2d(b), nn.ReLU(inplace=True),
        )

    class UNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.d1 = block(in_ch, base)
            self.d2 = block(base, base * 2)
            self.d3 = block(base * 2, base * 4)
            self.bott = block(base * 4, base * 8)
            self.pool = nn.MaxPool2d(2)
            self.u3 = nn.ConvTranspose2d(base * 8, base * 4, 2, stride=2)
            self.c3 = block(base * 8, base * 4)
            self.u2 = nn.ConvTranspose2d(base * 4, base * 2, 2, stride=2)
            self.c2 = block(base * 4, base * 2)
            self.u1 = nn.ConvTranspose2d(base * 2, base, 2, stride=2)
            self.c1 = block(base * 2, base)
            self.out = nn.Conv2d(base, out_ch, 1)

        def forward(self, x):
            d1 = self.d1(x)
            d2 = self.d2(self.pool(d1))
            d3 = self.d3(self.pool(d2))
            b = self.bott(self.pool(d3))
            x = self.c3(torch.cat([self.u3(b), d3], 1))
            x = self.c2(torch.cat([self.u2(x), d2], 1))
            x = self.c1(torch.cat([self.u1(x), d1], 1))
            return self.out(x)

    return UNet()


def _assemble_inputs(dem: np.ndarray, params: np.ndarray):
    """Build the (N, C, H, W) input tensor.

    Channels: normalised DEM, then one constant-valued channel per scenario
    parameter. Broadcasting a scalar across the grid is the standard way to
    condition a convolutional network on non-spatial inputs - the network can
    then modulate its spatial response by the scenario without any special
    architecture.
    """
    import torch

    ny, nx = dem.shape
    dem_n = (dem - np.nanmin(dem)) / max(np.nanmax(dem) - np.nanmin(dem), 1e-6)
    dem_n = np.nan_to_num(dem_n, nan=0.0)

    n = params.shape[0]
    chans = [np.broadcast_to(dem_n, (n, ny, nx))]
    for k, name in enumerate(PARAMS):
        v = (params[:, k] / PARAM_SCALE[name]).astype(np.float32)
        chans.append(np.broadcast_to(v[:, None, None], (n, ny, nx)))

    x = np.stack(chans, axis=1).astype(np.float32)
    return torch.from_numpy(np.ascontiguousarray(x))


def train(epochs: int = 120, batch: int = 2, lr: float = 2e-3, seed: int = 26161) -> dict:
    """Fit the emulator. Returns honest held-out metrics.

    The loss weights wet cells heavily. Most of the domain is dry in every
    sample, so an unweighted MSE is minimised by predicting "dry everywhere",
    which scores well and is useless.
    """
    import torch
    import torch.nn as nn

    blob = np.load(DATA_DIR / "dataset.npz")
    dem, depth, arrival, params = blob["dem"], blob["depth"], blob["arrival"], blob["params"]
    end_hr = float(blob["end_hr"])

    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    n = params.shape[0]
    idx = rng.permutation(n)
    n_test = max(int(0.2 * n), 2)
    test_i, train_i = idx[:n_test], idx[n_test:]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    X = _assemble_inputs(dem, params)

    depth_scale = float(np.percentile(depth, 99.9)) or 1.0
    # Three targets, not two: WHERE the water goes, then how deep and how soon.
    #
    # Only 0.52% of cells are ever wet - 167 of 32,256, and as few as 7 in some
    # samples. Against that imbalance a plain MSE on depth is minimised by
    # predicting dry everywhere, which scores beautifully and produced an
    # extent CSI of 0.012. Splitting out an explicit wet/dry channel lets the
    # loss below optimise overlap directly instead of hoping it falls out of a
    # regression on a mostly-zero field.
    wet_target = (depth >= 0.05).astype(np.float32)
    Y = torch.from_numpy(
        np.stack([wet_target, depth / depth_scale, arrival / end_hr], axis=1).astype(
            np.float32
        )
    )

    model = _build_unet(in_ch=X.shape[1], out_ch=3).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    mse = nn.MSELoss(reduction="none")

    Xtr, Ytr = X[train_i], Y[train_i]
    Xte, Yte = X[test_i].to(device), Y[test_i].to(device)

    t0 = time.perf_counter()
    for ep in range(epochs):
        model.train()
        order = torch.randperm(len(train_i))
        total = 0.0
        for k in range(0, len(order), batch):
            sel = order[k : k + batch]
            xb, yb = Xtr[sel].to(device), Ytr[sel].to(device)
            pred = model(xb)
            loss = _loss(pred, yb, mse)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            total += float(loss) * len(sel)
        sched.step()
        if (ep + 1) % 20 == 0:
            model.eval()
            with torch.no_grad():
                vl = float(_loss(model(Xte), Yte, mse))
            print(f"  epoch {ep + 1:>4}  train {total / len(order):.5f}  test {vl:.5f}")

    # --- honest evaluation, in physical units ---------------------------
    model.eval()
    with torch.no_grad():
        raw = model(Xte)
        wet_p = torch.sigmoid(raw[:, 0]).cpu().numpy()
        pred = raw.cpu().numpy()
    truth = Yte.cpu().numpy()

    # Depth is only meaningful where the network says it is wet. Multiplying by
    # the wet mask is what makes the two heads one prediction again.
    mask = wet_p >= 0.5
    d_pred = np.clip(pred[:, 1], 0, None) * depth_scale * mask
    d_true = truth[:, 1] * depth_scale
    a_pred = np.clip(pred[:, 2], 0, None) * end_hr
    a_true = truth[:, 2] * end_hr

    wet = d_true >= 0.05
    metrics = {
        "n_train": int(len(train_i)),
        "n_test": int(n_test),
        "device": str(device),
        "depth_mae_m_wet": float(np.abs(d_pred - d_true)[wet].mean()) if wet.any() else None,
        "depth_rmse_m_wet": float(np.sqrt(((d_pred - d_true) ** 2)[wet].mean())) if wet.any() else None,
        "arrival_mae_hr_wet": float(np.abs(a_pred - a_true)[wet].mean()) if wet.any() else None,
        "extent_csi": _extent_csi(d_pred, d_true),
        "train_seconds": round(time.perf_counter() - t0, 1),
        "target": "the fast solver's own output, NOT observed floods",
    }

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "state_dict": model.state_dict(),
            "in_ch": X.shape[1],
            # Store the head size. It was 2 in the first design and is 3 now
            # (wet / depth / arrival); inference rebuilt the old shape from a
            # default argument and died on a size mismatch. Anything the
            # architecture needs to be reconstructed belongs in the checkpoint,
            # not in a default that two call sites have to agree on.
            "out_ch": 3,
            "depth_scale": depth_scale,
            "end_hr": end_hr,
            "dem_shape": list(dem.shape),
            "params": list(PARAMS),
            "metrics": metrics,
        },
        MODEL_PATH,
    )
    (MODEL_DIR / "surrogate_metrics.json").write_text(json.dumps(metrics, indent=2))
    return metrics


def _loss(pred, target, mse):
    """Dice + BCE on the wet mask, masked MSE on depth and arrival.

    The Dice term is the one that matters. Dice is 2|A n B| / (|A| + |B|),
    which is a smooth stand-in for the Critical Success Index we actually
    report - so the network is optimising the metric it will be judged on
    rather than a proxy that happens to be easy to differentiate.

    Depth and arrival errors are computed ONLY on truly-wet cells. Asking the
    network to regress a depth for the 99.5% of the domain that is dry teaches
    it nothing and drowns the signal.
    """
    import torch
    import torch.nn.functional as F

    wet_logit, depth_p, arr_p = pred[:, 0], pred[:, 1], pred[:, 2]
    wet_t, depth_t, arr_t = target[:, 0], target[:, 1], target[:, 2]

    bce = F.binary_cross_entropy_with_logits(wet_logit, wet_t)

    prob = torch.sigmoid(wet_logit)
    inter = (prob * wet_t).sum(dim=(1, 2))
    dice = 1.0 - (2.0 * inter + 1.0) / (
        prob.sum(dim=(1, 2)) + wet_t.sum(dim=(1, 2)) + 1.0
    )

    m = wet_t
    denom = m.sum() + 1.0
    depth_l = (mse(depth_p, depth_t) * m).sum() / denom
    arr_l = (mse(arr_p, arr_t) * m).sum() / denom

    return bce + dice.mean() + 5.0 * depth_l + 2.0 * arr_l


def _extent_csi(d_pred: np.ndarray, d_true: np.ndarray, thr: float = 0.05) -> float:
    """Critical Success Index of the predicted wet extent against the solver's.

    Same metric as the satellite validation, so the two numbers are directly
    comparable - and it is the metric that matters for a flood map, where being
    right about WHERE is worth more than being right about how deep.
    """
    p, t = d_pred >= thr, d_true >= thr
    tp = int((p & t).sum())
    denom = tp + int((p & ~t).sum()) + int((~p & t).sum())
    return round(tp / denom, 4) if denom else 0.0


_LOADED: dict | None = None


def _load_once():
    """Load the network and the terrain once per process, then keep them.

    Reloading a checkpoint and re-initialising CUDA on every call cost 2.0
    SECONDS per prediction - which makes an emulator built for interactivity
    slower than useful and only 10x faster than the solver it replaces. Warm,
    it answers in about 15 ms. The whole justification for this module is that
    gap, so the cache is not an optimisation, it is the feature.
    """
    global _LOADED
    if _LOADED is not None:
        return _LOADED

    import torch

    ckpt = torch.load(MODEL_PATH, map_location="cpu", weights_only=False)
    blob = np.load(DATA_DIR / "dataset.npz")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    model = _build_unet(in_ch=ckpt["in_ch"], out_ch=ckpt.get("out_ch", 3)).to(device)
    model.load_state_dict(ckpt["state_dict"])
    model.eval()

    _LOADED = {"model": model, "ckpt": ckpt, "dem": blob["dem"], "device": device}
    return _LOADED


def predict(params: dict) -> dict:
    """Emulate one scenario. Returns depth and arrival grids in physical units."""
    import torch

    state = _load_once()
    model, ckpt, dem, device = (
        state["model"], state["ckpt"], state["dem"], state["device"]
    )

    vec = np.array([[params[p] for p in PARAMS]], dtype=np.float32)
    x = _assemble_inputs(dem, vec)

    t0 = time.perf_counter()
    with torch.no_grad():
        out = model(x.to(device)).cpu().numpy()[0]
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    wet = 1.0 / (1.0 + np.exp(-out[0])) >= 0.5
    return {
        "max_depth": np.clip(out[1], 0, None) * ckpt["depth_scale"] * wet,
        "arrival_time": np.clip(out[2], 0, None) * ckpt["end_hr"],
        "wet_mask": wet,
        "inference_ms": round(elapsed_ms, 2),
        "is_emulated": True,
    }


# ==========================================================================
# CLI
# ==========================================================================


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.07_ml.surrogate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dataset"); d.add_argument("--n", type=int, default=60)
    t = sub.add_parser("train")
    t.add_argument("--epochs", type=int, default=120)
    t.add_argument("--batch", type=int, default=2)
    sub.add_parser("check")
    args = ap.parse_args(argv)

    if args.cmd == "dataset":
        print(json.dumps(build_dataset(n=args.n), indent=2))
    elif args.cmd == "train":
        print(json.dumps(train(epochs=args.epochs, batch=args.batch), indent=2))
    else:
        out = predict({"reservoir_level_frac": 1.0, "capacity_mcm": 5.0,
                       "dam_height_m": 60.0, "formation_time_hr": 0.5})
        d = out["max_depth"]
        print(f"inference {out['inference_ms']} ms   "
              f"wet cells {(d >= 0.05).sum()}   max depth {d.max():.2f} m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
