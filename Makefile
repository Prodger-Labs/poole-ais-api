.PHONY: help deploy verify

help:
	@echo "make deploy   Publish or update the API on Gravitee"
	@echo "make verify   Check the deployed API answers, as REST and as MCP"

deploy:
	@python3 deploy.py

verify:
	@python3 verify.py
