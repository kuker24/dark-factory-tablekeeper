# Run

```sh
docker build -t tablekeeper-s1 .
docker run --rm -p 8080:8080 -e PORT=8080 tablekeeper-s1
```

Then point your client at `http://localhost:8080`. The image uses only the Python 3.12 standard library, no external services, no outbound network access at runtime.
