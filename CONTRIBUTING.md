# Contributing to GF Opti Crawler

GF Opti Crawler is an unofficial, experimental crawler **specific to GialloZafferano**. It is not validated on other sites. Contributions should preserve that scope unless compatibility is independently demonstrated.

## Local development

1. Use Python 3.12 and install `requirements.txt` in a virtual environment.
2. Run `python -m unittest discover -p 'test_*.py' -v` before opening a pull request.
3. Add synthetic HTML, mocked responses, or in-memory SQLite tests for bug fixes. Never include downloaded pages, recipes, images or private databases as fixtures.
4. Do not add automated live crawling to CI. Keep network-dependent testing explicitly opt-in, and honor site rules and access restrictions.
5. Describe behavioral changes, test evidence, and limitations in the pull request.

Some legacy tests automatically skip unless you supply two **local, non-committed** HTML fixtures (`Lasagne-al-forno.html` and `Hummus-senza-tahina.html`). They are not included in the public repository because they contain third-party site content.

## Security and responsible use

Do not commit credentials, session tokens, `.env`, databases or scraped exports. Respect robots.txt, applicable site terms and intellectual property rights. Robots.txt compliance alone does not grant permission to collect or redistribute content. Do not implement access-control bypasses, evasion or aggressive retry behavior.

## License

By contributing, you agree that contributions are distributed under the repository's MIT license. This license covers the source code only, not third-party website content.
