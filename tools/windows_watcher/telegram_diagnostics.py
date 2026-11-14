"""Codes fermes : aucun texte distant, credential, chemin ou URL dans les sorties."""
MESSAGES = {
    'INTERNAL_ERROR': 'Erreur locale non classee.',
    'VALIDATION_REFUSED': 'Controle local refuse.',
    'CREDENTIAL_PATH': 'Emplacement du credential refuse.',
    'CREDENTIAL_READ': 'Credential absent ou inaccessible.',
    'CREDENTIAL_ACL': 'Inspection ACL refusee ou indisponible.',
    'CREDENTIAL_CHANGED': 'Credential modifie pendant la lecture.',
    'CREDENTIAL_FORMAT': 'Credential vide ou format invalide.',
    'DNS_ERROR': 'Resolution DNS echouee.',
    'TLS_ERROR': 'Controle ou negotiation TLS echoue.',
    'NETWORK_TIMEOUT': 'Delai reseau depasse.',
    'NETWORK_CONNECTION': 'Connexion interrompue ou impossible.',
    'RESPONSE_INTERRUPTED': 'Lecture de reponse interrompue.',
    'NETWORK_UNCONFIRMED': 'Tentative reseau non confirmee.',
    'HTTP_REDIRECT': 'Redirection HTTP refusee.',
    'HTTP_400': 'Requete refusee par API ; cause detaillee non exposee.',
    'HTTP_401': 'Authentification refusee.',
    'HTTP_403': 'Acces refuse par API.',
    'HTTP_404': 'Ressource API introuvable.',
    'HTTP_429': 'Limitation API ; aucun retry.',
    'HTTP_OTHER': 'Reponse HTTP en echec.',
    'API_INVALID': 'Reponse API invalide ou trop volumineuse.',
    'BOT_IDENTITY': 'Identite du bot incorrecte.',
    'CHANNEL_IDENTITY': 'Canal prive attendu non concordant.',
    'ADMIN_PERMISSION': 'Statut administrateur ou droit de publication insuffisant.',
    'ROUTE_REFUSED': 'Route ou parametres non autorises.',
}


class DiagnosticError(Exception):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in MESSAGES else 'VALIDATION_REFUSED'
        super().__init__(MESSAGES[self.code])


def http_code(status):
    return 'HTTP_' + str(status) if type(status) is int and status in (400, 401, 403, 404, 429) else 'HTTP_OTHER'


def error_code(error):
    return error.code if (isinstance(error, DiagnosticError) and isinstance(error.code, str)
                          and error.code in MESSAGES) else 'INTERNAL_ERROR'
