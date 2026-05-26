"""
LineCalibrator — esqueleto do modelo aprendido (Fase 7).

Quando tivermos 2-3 semanas de log + ground truth do Bet365 populados
no `line_log.jsonl`, treinar um regressor (LightGBM) que substitui as
heurísticas hardcoded de `calculate_estimated_sportsbook_line` por
predições aprendidas dos dados.

ATENÇÃO: este é o ESQUELETO. As funções `train()` e `predict()` levantam
NotImplementedError quando chamadas — implementar quando a infra de log +
scrape do Bet365 estiver alimentando o JSONL.

Fluxo previsto:
  1. coletar dados (Fase 6 ligada por 2-3 semanas + scrape diário)
  2. carregar JSONL → DataFrame
  3. dividir em train/val/test temporal (NÃO random — vazamento)
  4. extrair features do `features` field (LineContext serializado)
  5. label = `bet365_line` quando presente
  6. treinar LightGBM com early stopping
  7. salvar modelo em CACHE_DIR/line_model.pkl
  8. wrapper que carrega no boot e expõe `predict(features)`

API pública (estável desde já):
  calibrator = LineCalibrator()
  calibrator.train(jsonl_path)              # treina e salva
  calibrator.load(model_path)                # carrega modelo salvo
  calibrator.predict(features) -> float      # inferência

Quando ativo, LineEngine pode delegar pro modelo:
  if calibrator.is_loaded:
      learned = calibrator.predict(ctx)
      # blend com heurística pra robustez
      line = 0.5 * heuristic_line + 0.5 * learned
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Mínimo de observações com ground truth (bet365_line) pra treinar.
MIN_TRAINING_SAMPLES = 1500


class LineCalibrator:
    """
    Wrapper sobre modelo aprendido (LightGBM regressor).

    Estado:
      _model: modelo treinado, None se não carregado
      _feature_names: ordem das features expected pelo modelo

    Hoje retorna NotImplementedError em train/predict — substitui quando
    tivermos dataset + decidir entre LightGBM/XGBoost/sklearn.
    """

    def __init__(self) -> None:
        self._model: Optional[Any] = None
        self._feature_names: list[str] = []

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self, model_path: str) -> bool:
        """
        Carrega modelo salvo. Returns True se OK, False se arquivo
        não existe ou está corrompido (caller cai pra heurística).
        """
        if not os.path.exists(model_path):
            logger.info("LineCalibrator: modelo %s não existe", model_path)
            return False
        try:
            import pickle
            with open(model_path, "rb") as f:
                data = pickle.load(f)
            self._model = data["model"]
            self._feature_names = data["feature_names"]
            logger.info("LineCalibrator: modelo carregado (%d features)", len(self._feature_names))
            return True
        except Exception as exc:
            logger.warning("LineCalibrator: load falhou (%s)", exc)
            return False

    def train(self, jsonl_path: str, output_path: Optional[str] = None) -> dict:
        """
        Treina modelo a partir do log estruturado.

        Pipeline previsto (implementar quando dataset for suficiente):
          1. Carrega JSONL via streaming (pandas read_json com lines=True)
          2. Filtra registros com bet365_line não-null
          3. Valida MIN_TRAINING_SAMPLES
          4. Extrai features numéricas do `features` field
          5. Split temporal (train: < D-7d, val: últimos 7d)
          6. lgb.LGBMRegressor com early stopping em val
          7. Salva pickle com {model, feature_names, train_metrics}
          8. Retorna métricas (MAE train/val/test)

        Args:
          jsonl_path: caminho pro line_log.jsonl
          output_path: onde salvar (default CACHE_DIR/line_model.pkl)

        Returns:
          dict com métricas do treino, ou {"error": "..."} se falhou.
        """
        raise NotImplementedError(
            "LineCalibrator.train ainda não implementado. "
            f"Pré-requisitos: (1) LOG_LINE_CALC=1 ligado e gerando dados, "
            f"(2) bet365_line populado em ≥ {MIN_TRAINING_SAMPLES} registros, "
            f"(3) dependência lightgbm instalada."
        )

    def predict(self, features: dict) -> float:
        """
        Inferência. Espera dict com mesma chave-shape do `features` no log.

        Returns:
          line previsto. NotImplementedError se modelo não carregado.
        """
        if not self.is_loaded:
            raise NotImplementedError(
                "LineCalibrator.predict requer modelo carregado. "
                "Chame load() primeiro ou rode train() pra criar."
            )
        # Quando implementar:
        #   X = [features.get(name, 0) for name in self._feature_names]
        #   return float(self._model.predict([X])[0])
        raise NotImplementedError("predict() pendente — modelo carregado mas sem inferência implementada")

    @staticmethod
    def estimate_dataset_size(jsonl_path: str) -> dict:
        """
        Helper que conta quantos registros disponíveis e quantos com
        bet365_line populado. Útil pra saber se já dá pra treinar.
        """
        if not os.path.exists(jsonl_path):
            return {"available": False, "path": jsonl_path}
        total = 0
        with_bet365 = 0
        with_outcome = 0
        try:
            import json
            with open(jsonl_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total += 1
                    if rec.get("bet365_line") is not None:
                        with_bet365 += 1
                    if rec.get("actual_outcome") is not None:
                        with_outcome += 1
        except Exception as exc:
            return {"available": False, "error": str(exc)}
        return {
            "available": True,
            "total_records": total,
            "with_bet365_label": with_bet365,
            "with_outcome": with_outcome,
            "min_required": MIN_TRAINING_SAMPLES,
            "ready_to_train": with_bet365 >= MIN_TRAINING_SAMPLES,
        }
