"""The routing test matrix. Labels are the intent a person means, not a guess of the model.

``generate``   an explicit request for a new picture      -> generate_image exactly once
``chat``       ordinary conversation                      -> no image call
``ambiguous``  mentions pictures without asking for one   -> classified, not scored as right/wrong
``attached_describe``  an image is attached, question about it -> no generate/edit
``attached_edit``      an image is attached, change requested   -> edit_image (when offered)
"""

from __future__ import annotations

GENERATE = [
    "Generate an image of a red apple on a white table.",
    "generate an image of a lighthouse at sunset",
    "Generate a picture of a cat wearing a tiny wizard hat.",
    "Can you generate an image of a mountain lake in autumn?",
    "Draw a friendly robot waving hello.",
    "draw a dragon flying over a medieval castle",
    "Please draw a bowl of ramen, top-down view.",
    "Create a picture of a futuristic city skyline at night.",
    "create a picture of two puppies playing in the snow",
    "Create an image of a cozy reading nook with a fireplace.",
    "Make me an image of a vintage red bicycle leaning on a wall.",
    "make me a picture of a sunflower field under a blue sky",
    "Make an illustration of a spaceship landing on Mars.",
    "I want an image of a koi pond with lily pads.",
    "Produce an image of a steaming cup of coffee on a wooden desk.",
    "Show me a picture of a snowy cabin in the woods, generated.",
    "Paint a watercolor of a Venetian canal.",
    "Render an image of a glass sphere reflecting a sunset.",
    "Gere uma imagem de uma maçã vermelha sobre uma mesa branca.",
    "gere uma imagem de um farol ao pôr do sol",
    "Desenhe um gato usando um chapéu de mago.",
    "desenhe um robô simpático acenando",
    "Crie uma foto de uma cidade futurista à noite.",
    "crie uma foto de dois cachorrinhos brincando na neve",
    "Faça uma imagem de uma bicicleta vermelha antiga encostada na parede.",
    "faça uma imagem de um campo de girassóis sob um céu azul",
    "Crie uma imagem de uma nave espacial pousando em Marte.",
    "Gera uma ilustração de um dragão sobrevoando um castelo.",
    "Quero uma imagem de uma xícara de café fumegante numa mesa de madeira.",
    "Pode gerar uma imagem de um lago na montanha no outono?",
]

CHAT = [
    "hi",
    "Hello!",
    "bom dia",
    "oi, tudo bem?",
    "What is 2+2?",
    "quanto é 17 vezes 23?",
    "Explain recursion in one short paragraph.",
    "Tell me a joke.",
    "me conta uma piada",
    "What's the capital of Australia?",
    "Qual é a capital do Canadá?",
    "Give me three tips to sleep better.",
    "Translate 'good morning' to Spanish.",
    "How do I reverse a list in Python?",
    "What is the difference between TCP and UDP?",
    "Write a haiku about the ocean.",
    "Escreva um haicai sobre a chuva.",
    "Summarize the plot of Romeo and Juliet in two sentences.",
    "What year did humans first land on the Moon?",
    "Recommend a good sci-fi book.",
    "Como faço café coado?",
    "What does HTTP stand for?",
    "Convert 100 Fahrenheit to Celsius.",
    "Is a tomato a fruit or a vegetable?",
    "thanks, that helped",
    "valeu!",
    "What is the boiling point of water at sea level?",
    "List the planets of the solar system.",
    "Explique o que é uma API em uma frase.",
    "Can you help me write an email asking for a day off?",
]

AMBIGUOUS = [
    "imagine a castle",
    "Describe a red apple.",
    "How would you draw a cat?",
    "What makes a good portrait?",
    "What colors go well with a sunset painting?",
    "Picture this: a quiet beach at dawn. How does it feel?",
    "Tell me about the Mona Lisa.",
    "What is a good prompt for generating a fantasy landscape?",
    "Describe what a futuristic city might look like.",
    "What's the best camera setting for night photos?",
    "imagine um castelo",
    "Descreva uma maçã vermelha.",
    "Como você desenharia um gato?",
    "O que faz um bom retrato?",
    "Me dá ideias de desenho para iniciantes.",
    "Qual a diferença entre aquarela e óleo?",
    "Descreva uma floresta encantada.",
    "Como tirar uma boa foto de comida?",
    "Explique como funciona a geração de imagens por IA.",
    "Que prompt eu usaria para gerar um logotipo?",
]

ATTACHED_DESCRIBE = [
    "What is in this picture?",
    "explain this image",
    "Describe this picture.",
    "What is shown here?",
    "O que tem nessa imagem?",
    "Descreva esta foto.",
]

ATTACHED_EDIT = [
    "Change the background to black.",
    "make the apple green",
    "Remove the background.",
    "Add a red hat to it.",
    "Mude o fundo para preto.",
    "Deixe a imagem em preto e branco.",
]

SUITES = {
    "generate": GENERATE,
    "chat": CHAT,
    "ambiguous": AMBIGUOUS,
    "attached_describe": ATTACHED_DESCRIBE,
    "attached_edit": ATTACHED_EDIT,
}

assert len(GENERATE) >= 30 and len(CHAT) >= 30 and len(AMBIGUOUS) >= 20
