# IFC Processor

Serviço FastAPI + IfcOpenShell que processa arquivos IFC para a plataforma Read Emissions.

## Rodar localmente

```bash
pip install -r requirements.txt
export SERVICE_TOKEN="uma-senha-forte-que-voce-escolhe"
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Deploy no Render (gratuito)

1. Crie uma conta em https://render.com
2. Suba esta pasta para um repositório no GitHub
3. No Render: **New → Web Service → conecte o repositório**
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Em **Environment**, adicione `SERVICE_TOKEN` com uma senha forte
7. Anote a URL gerada (ex.: `https://ifc-processor.onrender.com`)

Depois configure na plataforma os segredos `IFC_PROCESSOR_URL` e `IFC_PROCESSOR_TOKEN`.

> Observação: o plano gratuito do Render "adormece" após inatividade (o primeiro
> processamento pode demorar ~1 min para acordar) e tem 512 MB de RAM — arquivos
> IFC muito grandes podem exigir um plano pago.
