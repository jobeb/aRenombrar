# =============================================================
# Script para crear el repositorio GitHub de aRenombrar
# Ejecutar desde la carpeta del proyecto:
#   powershell -ExecutionPolicy Bypass -File crear_repo_github.ps1
# =============================================================

$ErrorActionPreference = "Stop"
$RepoName = "aRenombrar"
$Descripcion = "Aplicacion de escritorio para renombrar capitulos y peliculas automaticamente via TMDB y subir por FTP"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "`n=== Configurando repositorio GitHub: $RepoName ===" -ForegroundColor Cyan

# 1. Inicializar git
Set-Location $ProjectDir
if (-not (Test-Path ".git")) {
    Write-Host "`n[1/4] Inicializando git..." -ForegroundColor Yellow
    git init -b main
    git config user.email "desarrolladorjose@gmail.com"
    git config user.name "Jose"
} else {
    Write-Host "`n[1/4] Git ya inicializado." -ForegroundColor Green
}

# 2. Primer commit
Write-Host "`n[2/4] Creando commit inicial..." -ForegroundColor Yellow
git add -A
git status --short
git commit -m "feat: version inicial de aRenombrar

- Renombrado automatico de series y peliculas via TMDB
- Subida FTP con soporte de tildes (Latin-1/UTF-8 auto)
- Cola de subida dinamica con progreso y velocidad
- Historial de subidas y notificaciones de escritorio
- Interfaz CustomTkinter con tema oscuro"

# 3. Crear repo en GitHub
Write-Host "`n[3/4] Creando repositorio en GitHub..." -ForegroundColor Yellow

$ghAvailable = Get-Command gh -ErrorAction SilentlyContinue
if ($ghAvailable) {
    Write-Host "  Usando GitHub CLI (gh)..."
    gh repo create $RepoName --public --description $Descripcion --source . --remote origin --push
    Write-Host "`n[4/4] Repo creado y codigo subido!" -ForegroundColor Green
} else {
    Write-Host "  GitHub CLI no encontrado. Usando API con token personal..." -ForegroundColor Yellow
    $Token = Read-Host "  Introduce tu GitHub Personal Access Token (scope: repo)"
    $Username = Read-Host "  Introduce tu usuario de GitHub"

    $Body = @{
        name        = $RepoName
        description = $Descripcion
        private     = $false
        auto_init   = $false
    } | ConvertTo-Json

    $Headers = @{
        Authorization = "token $Token"
        Accept        = "application/vnd.github+json"
    }

    try {
        $Response = Invoke-RestMethod -Uri "https://api.github.com/user/repos" `
            -Method Post -Body $Body -Headers $Headers -ContentType "application/json"
        $RemoteUrl = $Response.clone_url
        Write-Host "  Repo creado: $RemoteUrl" -ForegroundColor Green

        # 4. Push
        Write-Host "`n[4/4] Subiendo codigo a GitHub..." -ForegroundColor Yellow
        git remote add origin $RemoteUrl
        git push -u origin main
        Write-Host "`nListo! Repositorio disponible en: $($Response.html_url)" -ForegroundColor Green
    } catch {
        Write-Host "  Error al crear repo: $_" -ForegroundColor Red
        Write-Host "  Crea el repo manualmente en https://github.com/new y ejecuta:" -ForegroundColor Yellow
        Write-Host "    git remote add origin https://github.com/TU_USUARIO/$RepoName.git"
        Write-Host "    git push -u origin main"
    }
}
