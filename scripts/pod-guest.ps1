<#
.SYNOPSIS
  Sobe a Capella num pod Linux alugado e abre um link público para um convidado.

.DESCRIPTION
  Roda no Windows, a partir do checkout. Usa o runbook Linux (docs/testing/runbook-linux-1.17.md)
  em ordem, por SSH:

    -Step setup   envia e confere o pacote, instala, inicia, abre o seu túnel, espera você criar
                  a conta admin e então abre o link público (share). Padrão.
    -Step share   só reabre o seu túnel e o link público (pod já instalado).
    -Step swaplab envia Downloads\swap-lab (cases.json + inputs), roda o laboratório de troca de
                  personagem no pod e traz os resultados para Downloads\swap-lab\results-pod-<data>.
    -Step finish  encerra o link e os serviços e traz a sessão para Downloads. Depois disso,
                  exclua o pod no painel do provedor.

  Na primeira conexão, o ssh pergunta se confia na chave do servidor: confira e responda yes.
  O token do Civitai é pedido pelo instalador, com entrada oculta.

.EXAMPLE
  .\scripts\pod-guest.ps1 -SshHost <usuario>@<host> -SshPort <porta>
.EXAMPLE
  .\scripts\pod-guest.ps1 -SshHost <usuario>@<host> -SshPort <porta> -Step finish
#>
[CmdletBinding()]
param(
    [string]$SshHost,
    [int]$SshPort = 0,
    [string]$Identity = (Join-Path $HOME ".ssh\id_ed25519"),
    [ValidateSet("", "setup", "share", "swaplab", "finish")][string]$Step = "",
    [string]$Base = "/mnt/pepitos-bench",
    # Porta local do seu túnel: 18080, para não colidir com um Open WebUI local na 8080.
    [int]$LocalPort = 18080,
    [string]$SwapLab = (Join-Path $HOME "Downloads\swap-lab")
)
$ErrorActionPreference = "Stop"

# Sem parâmetros (duplo clique em Pod-Convidado.cmd): pergunta o que precisa.
if (-not $Step -and $SshHost) { $Step = "setup" }
if (-not $Step) {
    Write-Host "1) Subir o pod e abrir o link (primeira vez)"
    Write-Host "2) Só reabrir o link (pod já instalado)"
    Write-Host "3) Rodar o laboratório de troca de personagem (pod já instalado)"
    Write-Host "4) Encerrar: trazer a sessão e liberar para excluir o pod"
    $Step = @{ "1" = "setup"; "2" = "share"; "3" = "swaplab"; "4" = "finish" }[(Read-Host "Escolha 1 a 4").Trim()]
    if (-not $Step) { throw "Opção inválida." }
}
if (-not $SshHost) { $SshHost = (Read-Host "Host SSH do pod (ex.: <usuario>@<host>)").Trim() }
if (-not $SshPort) {
    $answer = (Read-Host "Porta SSH do pod (Enter = 22)").Trim()
    $SshPort = if ($answer) { [int]$answer } else { 22 }
}
if (-not (Test-Path $Identity)) { throw "Chave SSH não encontrada: $Identity" }

$Hub = Split-Path -Parent $PSScriptRoot
$HubPython = Join-Path $Hub ".venv\Scripts\python.exe"
$Version = (& $HubPython -c "import local_ai_stack; print(local_ai_stack.__version__)").Trim()
$Release = "PepitosAIHub-Capella-v$Version-linux-amd64"
$Bundle = "$Base/$Release"
$Root = "$Base/capella-data"
$Ssh = @("-p", "$SshPort", "-i", $Identity, "-o", "ServerAliveInterval=30")
$TunnelPid = Join-Path $env:TEMP "pepito-pod-tunnel.pid"

function Say([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }

function Remote([string]$Command, [switch]$Tty) {
    $options = $Ssh
    if ($Tty) { $options = @("-t") + $Ssh }
    & ssh @options $SshHost $Command
    if ($LASTEXITCODE -ne 0) { throw "Falhou no pod (código $LASTEXITCODE): $Command" }
}

function Capella([string]$Action, [switch]$Tty) {
    Remote "cd '$Bundle' && bash scripts/capella-linux.sh $Action '$Root'" -Tty:$Tty
}

function Open-Tunnel {
    Close-Tunnel
    $arguments = @("-N", "-o", "ExitOnForwardFailure=yes") + $Ssh + @(
        "-L", "127.0.0.1:${LocalPort}:127.0.0.1:8080", $SshHost
    )
    $process = Start-Process ssh -ArgumentList $arguments -WindowStyle Minimized -PassThru
    Set-Content -Path $TunnelPid -Value $process.Id
    Start-Sleep -Seconds 3
    if ($process.HasExited) { throw "O túnel SSH não abriu (a porta $LocalPort está livre?)." }
}

function Close-Tunnel {
    if (Test-Path $TunnelPid) {
        $id = [int](Get-Content $TunnelPid)
        Get-Process -Id $id -ErrorAction SilentlyContinue | Stop-Process -Force
        Remove-Item $TunnelPid -Force
    }
}

function Share {
    Say "Abrindo o link público. Mande para ela o endereço https://...trycloudflare.com emoldurado."
    Write-Host "    Deixe esta janela aberta. Ctrl+C encerra o link." -ForegroundColor Yellow
    Capella "share" -Tty
}

switch ($Step) {
    "setup" {
        $Archive = Join-Path $Hub "dist\$Release.tar.gz"
        if (-not (Test-Path $Archive)) {
            Say "Gerando o pacote Linux $Version"
            & $HubPython (Join-Path $Hub "scripts\build_linux.py")
            if ($LASTEXITCODE -ne 0) { throw "build_linux.py falhou." }
        }
        $Expected = ((Get-Content "$Archive.sha256") -split "\s+")[0]
        $Actual = (Get-FileHash -Algorithm SHA256 $Archive).Hash.ToLowerInvariant()
        if ($Expected -ne $Actual) { throw "SHA256 do pacote local não confere com o recibo." }

        Say "Conectando (confira a chave do servidor se o ssh perguntar)"
        Remote "echo conectado: `$(uname -sm)"

        Say "Enviando o pacote $Release"
        & scp -P "$SshPort" -i $Identity $Archive "$Archive.sha256" "${SshHost}:/tmp/"
        if ($LASTEXITCODE -ne 0) { throw "scp falhou." }

        Say "Conferindo e extraindo no pod"
        Remote "cd /tmp && sha256sum -c '$Release.tar.gz.sha256' && mkdir -p '$Base' && tar -xzf '/tmp/$Release.tar.gz' -C '$Base'"

        Say "Dependências do sistema, ambiente do Hub e verificação da GPU"
        Remote "cd '$Bundle' && `$( [ `$(id -u) = 0 ] || echo sudo ) bash scripts/capella-linux.sh deps && bash scripts/capella-linux.sh prepare"
        Capella "check"

        # Os tokens salvos no Hub (Configurações → Civitai / Hugging Face, DPAPI) vão só pelo
        # stdin do ssh: nunca em argumento, arquivo ou log. Sem token salvo, o instalador pede.
        $Tokens = & $HubPython -c "from local_ai_stack import civitai_auth as c, huggingface_auth as h; print(c.load_token()[0] or ''); print(h.load_token()[0] or '')"
        if ($Tokens -and $Tokens[0]) {
            Say "Instalando a Capella com os tokens salvos no Hub (demora)"
            $Tokens | & ssh @Ssh $SshHost "IFS= read -r CIVITAI_TOKEN; IFS= read -r HF_TOKEN; export CIVITAI_TOKEN; [ -n `"`$HF_TOKEN`" ] && export HF_TOKEN; cd '$Bundle' && bash scripts/capella-linux.sh install '$Root' < /dev/null"
            if ($LASTEXITCODE -ne 0) { throw "A instalação falhou no pod (código $LASTEXITCODE)." }
        } else {
            Say "Instalando a Capella (cole o token do Civitai quando pedir; demora)"
            Capella "install" -Tty
        }
        Remove-Variable Tokens

        Say "Iniciando os serviços"
        Capella "start"

        Say "Abrindo o seu túnel em http://127.0.0.1:$LocalPort"
        Open-Tunnel
        Start-Process "http://127.0.0.1:$LocalPort"
        Write-Host ""
        Write-Host "    No navegador, crie AGORA a SUA conta (ela vira a administradora)." -ForegroundColor Yellow
        Read-Host "    Depois de criar e entrar, aperte Enter aqui"

        Say "Sincronizando filtros e modelos com a sua conta"
        Capella "start"
        Share
    }
    "share" {
        Open-Tunnel
        Share
    }
    "swaplab" {
        if (-not (Test-Path (Join-Path $SwapLab "cases.json"))) { throw "Falta $SwapLab\cases.json." }
        $RemoteLab = "$Base/swap-lab"
        Say "Enviando o laboratório de troca ($SwapLab)"
        Remote "rm -rf '$RemoteLab' && mkdir -p '$RemoteLab'"
        & scp -P "$SshPort" -i $Identity -r (Join-Path $SwapLab "cases.json") (Join-Path $SwapLab "inputs") "${SshHost}:$RemoteLab/"
        if ($LASTEXITCODE -ne 0) { throw "scp do laboratório falhou." }
        Say "Garantindo os serviços e rodando os métodos de troca"
        Capella "start"
        Remote "cd '$Bundle' && bash scripts/capella-linux.sh swap-lab '$Root' '$RemoteLab'"
        $Local = Join-Path $SwapLab ("results-pod-{0}" -f (Get-Date -Format "yyyyMMdd-HHmm"))
        Say "Trazendo os resultados para $Local"
        & scp -P "$SshPort" -i $Identity -r "${SshHost}:$RemoteLab/results" $Local
        if ($LASTEXITCODE -ne 0) { throw "scp dos resultados falhou; eles continuam em $RemoteLab/results no pod." }
        # The composite without cut marks runs here, on this PC, in its own throwaway
        # environment (never the Hub's .venv): torch CPU, OpenCV and MiDaS, ~1 min a run.
        $SwapVenv = Join-Path $env:LOCALAPPDATA "PepitosAIHub\swap-venv"
        $SwapPython = Join-Path $SwapVenv "Scripts\python.exe"
        if (-not (Test-Path $SwapPython)) {
            Say "Preparando o ambiente do compositor (uma vez, alguns minutos)"
            & $HubPython -m venv $SwapVenv
            & $SwapPython -m pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cpu
            & $SwapPython -m pip install -q numpy opencv-python-headless pillow timm
            if ($LASTEXITCODE -ne 0) { throw "Não consegui preparar o ambiente do compositor." }
        }
        $Composite = "$Local-composicao"
        Say "Compondo sem marcas de corte em $Composite"
        & $SwapPython (Join-Path $PSScriptRoot "swap_composite.py") --results $Local `
            --cases (Join-Path $SwapLab "cases.json") --out $Composite
        if ($LASTEXITCODE -eq 0) { Start-Process (Join-Path $Composite "avaliar-composicao.html") }
        else { Say "O compositor falhou; a troca continua em $Local" }
        Start-Process (Join-Path $Local "avaliar-troca.html")
        Say "Pronto: avalie em $Local\avaliar-troca.html e $Composite\avaliar-composicao.html"
    }
    "finish" {
        Say "Parando os serviços"
        Capella "stop"
        $Output = Join-Path $HOME ("Downloads\capella-pod-sessao-{0}.zip" -f (Get-Date -Format "yyyyMMdd-HHmm"))
        Say "Trazendo a sessão para $Output"
        & $HubPython -m local_ai_stack hub fetch-session --host $SshHost --port $SshPort `
            --identity-file $Identity --bundle $Bundle --root $Root --output $Output
        if ($LASTEXITCODE -ne 0) { throw "fetch-session falhou; não exclua o pod ainda." }
        Close-Tunnel
        Say "Pronto. Agora pode excluir o pod no painel do provedor."
    }
}
