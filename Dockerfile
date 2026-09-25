FROM node:22-alpine
WORKDIR /app
COPY server.js index.html ./
ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=8787
EXPOSE 8787
USER node
CMD ["node", "server.js"]
