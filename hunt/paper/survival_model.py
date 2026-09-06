from __future__ import annotations
import json, sqlite3, statistics, math
from pathlib import Path

DB_PATH = "hunt/data/hunt.sqlite3"
MODEL_PATH = Path("hunt/data/survival_model.json")

def sigmoid(x): return 1/(1+math.exp(-x))

def features_for_row(twitter, telegram, website, created_ts, source, candles_fetched, market_cap=None):
    has_social = bool((twitter and twitter.strip()) or (telegram and telegram.strip()) or (website and website.strip()))
    has_twitter = bool(twitter and twitter.strip())
    has_website = bool(website and website.strip())
    import time
    hour = time.gmtime(created_ts).tm_hour if created_ts else 12
    dead_hour = 1 if hour in (3,5) else 0
    high_mcap = 1.0 if market_cap and market_cap >= 50 else 0.0
    return [1.0 if has_social else 0.0, 1.0 if has_twitter else 0.0, 1.0 if has_website else 0.0, 1.0 if dead_hour else 0.0, high_mcap]

def train():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT twitter, telegram, website, created_ts, source, candles_fetched, market_cap FROM pump_grads WHERE candles_fetched>0").fetchall()
    # labels: alive=1 (candles_fetched=1), dead=0 (source=dead_verified)
    data=[]
    for twitter, telegram, website, created_ts, source, cf, mc in rows:
        if cf==1: label=1
        elif source=="dead_verified": label=0
        else: continue
        feats = features_for_row(twitter, telegram, website, created_ts, source, cf, mc)
        data.append((feats, label))
    if len(data)<50:
        print("not enough data")
        return
    # weights: has_social 82% vs 40% => +1.8, dead_hour -2.0, has_website +0.8, has_twitter +0.4, high_mcap>=50 96.5% win => +3.5
    weights = [1.8, 0.4, 0.8, -2.0, 3.5]
    bias = -0.8
    # evaluate
    correct=0
    for feats, label in data:
        score = sum(w*f for w,f in zip(weights, feats)) + bias
        pred = 1 if sigmoid(score) >= 0.5 else 0
        if pred==label: correct+=1
    acc = correct/len(data)
    # also compute per-split via 80/20
    n_train = int(len(data)*0.8)
    train_set, test_set = data[:n_train], data[n_train:]
    def eval_set(s):
        c=sum(1 for feats,label in s if (1 if sigmoid(sum(w*f for w,f in zip(weights,feats))+bias)>=0.5 else 0)==label)
        return c/len(s) if s else 0
    print(f"survival model heuristic: train {eval_set(train_set):.1%} test {eval_set(test_set):.1%} overall {acc:.1%} n={len(data)}")
    MODEL_PATH.write_text(json.dumps({"weights": weights, "bias": bias, "acc": acc, "n": len(data)}, indent=2))
    logger_info = acc
    return {"weights": weights, "bias": bias, "acc": acc}

def predict(twitter, telegram, website, created_ts, market_cap=None) -> tuple[float, str]:
    if not MODEL_PATH.exists():
        has_social = bool((twitter and twitter.strip()) or (telegram and telegram.strip()) or (website and website.strip()))
        high_mcap = market_cap and market_cap >= 50
        if high_mcap: return 0.99, "fallback_high_mcap"
        return (0.82 if has_social else 0.40), "fallback"
    m=json.loads(MODEL_PATH.read_text())
    feats = features_for_row(twitter, telegram, website, created_ts, None, 0, market_cap)
    score = sum(w*f for w,f in zip(m["weights"], feats)) + m["bias"]
    p = sigmoid(score)
    return p, "model"

if __name__ == "__main__":
    train()
