# Test Plan

## Unit
- profile loader/validation;
- compatibility rules;
- parsing `nvidia-smi`;
- install plan;
- paths.

## Integration
- subprocess runner;
- health checks;
- downloads temporários;
- git clone fixture/local;
- launcher de processo fake.

## Smoke Pepe
Windows detectado, NVIDIA detectada, profile carrega, Doctor não crasha com serviços desligados e install plan é determinístico.

## Smoke Capella em Pepe
Profile carrega, mas compatibility sinaliza VRAM insuficiente e UI permite voltar.

## Smoke Test
Compatível com Pepe, menor footprint e mesmas APIs.

## E2E futuro

`clean runtime -> install Pepe -> start -> health -> chat -> t2i -> image edit`.
