FROM plone/plone-backend:6.0.0b3

WORKDIR /app

RUN /app/bin/pip install git+https://github.com/pretagov/collective.elastic.git@mle-redis-rq#egg=collective.elastic[redis]

ENV PROFILES="collective.elastic:default collective.elastic:docker-dev"
ENV TYPE="classic"
ENV SITE="Plone"
