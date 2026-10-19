# helivex-paper:local — quant-nautilus + paper deps baked in (avoids 3-5 min pip at every recreate)
FROM quant-nautilus:latest
USER app
RUN pip install --user --no-cache-dir --no-warn-script-location pyyaml fastapi uvicorn nautilus_trader==1.228.0 \
 && python -c "import yaml,fastapi,uvicorn,nautilus_trader as n; assert n.__version__=='1.228.0', n.__version__; print('ok', n.__version__)"
# paper/audit.py signs audit records with Ed25519 once HELIVEX_AUDIT_PRIVATE_KEY_B64 is set
RUN pip install --user --no-cache-dir --no-warn-script-location cryptography \
 && python -c "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; print('crypto ok')"
