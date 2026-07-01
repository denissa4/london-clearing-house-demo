.PHONY: data test run run-http inspect clean
data:            ## generate sample report CSVs
	python scripts/generate_samples.py
test: data       ## run tool smoke tests
	python scripts/test_tools.py
run:             ## run MCP server over stdio
	python mcp_server/server.py
run-http:        ## run MCP server over HTTP on :8080
	MCP_TRANSPORT=http MCP_PORT=8080 python mcp_server/server.py
inspect:         ## open MCP Inspector against the server
	npx @modelcontextprotocol/inspector python mcp_server/server.py
clean:           ## remove generated + cache files
	rm -rf data/*.csv __pycache__ */__pycache__ sftp_root
