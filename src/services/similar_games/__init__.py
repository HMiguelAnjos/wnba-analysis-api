"""
SimilarGameAnalyzer — busca jogos históricos com início similar ao atual.

Resposta direta à pergunta: "quando esse jogador teve um Q1 assim,
o que aconteceu nos últimos N casos similares?"

Útil pra validar entradas tipo "KJ tá 2 pts em 8 min — OVER 6.5 vai
virar ou ele continua frio?". O recovery_factor agregado diz o caminho.
"""
from src.services.similar_games.analyzer import SimilarGameAnalyzer

__all__ = ["SimilarGameAnalyzer"]
