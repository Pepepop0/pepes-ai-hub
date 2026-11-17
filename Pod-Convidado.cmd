@echo off
rem Sobe a Capella num pod alugado e abre um link publico para um convidado.
rem Duplo clique: pergunta o que fazer, o host e a porta SSH. Detalhes: docs\testing\runbook-linux-1.17.md
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\pod-guest.ps1" %*
pause
