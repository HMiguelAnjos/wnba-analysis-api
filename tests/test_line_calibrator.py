"""
Testes do esqueleto LineCalibrator (Fase 7).

Cobertura:
- predict() sem modelo carregado → NotImplementedError
- train() sem dependências → NotImplementedError com mensagem útil
- load() com path inexistente → False
- estimate_dataset_size() com vários cenários
"""

import json
import os
import tempfile

import pytest

from src.services.line.line_calibrator import LineCalibrator


def test_calibrator_predict_without_model_raises():
    c = LineCalibrator()
    with pytest.raises(NotImplementedError, match="modelo carregado"):
        c.predict({})


def test_calibrator_train_not_yet_implemented():
    c = LineCalibrator()
    with pytest.raises(NotImplementedError, match="LOG_LINE_CALC"):
        c.train("/tmp/nonexistent.jsonl")


def test_calibrator_load_nonexistent_returns_false():
    c = LineCalibrator()
    assert c.load("/tmp/this_path_does_not_exist.pkl") is False
    assert not c.is_loaded


def test_estimate_dataset_size_no_file():
    info = LineCalibrator.estimate_dataset_size("/tmp/never_existed.jsonl")
    assert info["available"] is False


def test_estimate_dataset_size_counts_records():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "line_log.jsonl")
    records = [
        {"our_line": 20, "bet365_line": 21, "actual_outcome": 22},
        {"our_line": 15, "bet365_line": None, "actual_outcome": 16},
        {"our_line": 25, "bet365_line": 26, "actual_outcome": None},
        {"our_line": 30},  # nada populado
    ]
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    info = LineCalibrator.estimate_dataset_size(path)
    assert info["available"] is True
    assert info["total_records"] == 4
    assert info["with_bet365_label"] == 2
    assert info["with_outcome"] == 2
    assert info["ready_to_train"] is False  # < 1500


def test_estimate_dataset_size_handles_malformed():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "line_log.jsonl")
    with open(path, "w") as f:
        f.write(json.dumps({"our_line": 20, "bet365_line": 21}) + "\n")
        f.write("not valid json\n")
        f.write(json.dumps({"our_line": 22, "bet365_line": 23}) + "\n")
    info = LineCalibrator.estimate_dataset_size(path)
    assert info["total_records"] == 2  # malformed pulado
    assert info["with_bet365_label"] == 2
