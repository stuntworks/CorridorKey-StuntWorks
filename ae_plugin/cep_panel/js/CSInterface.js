/**
 * CSInterface - Adobe CEP JavaScript Library (Minimal Version)
 * For full version, download from Adobe CEP Resources
 */

function CSInterface() {}

CSInterface.prototype.evalScript = function(script, callback) {
    if (window.__adobe_cep__) {
        window.__adobe_cep__.evalScript(script, callback);
    }
};

CSInterface.prototype.getSystemPath = function(pathType) {
    var path = "";
    if (window.__adobe_cep__) {
        path = window.__adobe_cep__.getSystemPath(pathType);
    }
    return path;
};

CSInterface.prototype.addEventListener = function(type, listener, obj) {
    if (window.__adobe_cep__) {
        window.__adobe_cep__.addEventListener(type, listener, obj);
    }
};

CSInterface.prototype.removeEventListener = function(type, listener, obj) {
    if (window.__adobe_cep__) {
        window.__adobe_cep__.removeEventListener(type, listener, obj);
    }
};

CSInterface.prototype.requestOpenExtension = function(extensionId, params) {
    if (window.__adobe_cep__) {
        window.__adobe_cep__.requestOpenExtension(extensionId, params);
    }
};

CSInterface.prototype.closeExtension = function() {
    if (window.__adobe_cep__) {
        window.__adobe_cep__.closeExtension();
    }
};

CSInterface.prototype.getHostEnvironment = function() {
    var hostEnv = null;
    if (window.__adobe_cep__) {
        hostEnv = JSON.parse(window.__adobe_cep__.getHostEnvironment());
    }
    return hostEnv;
};

// System path constants
CSInterface.prototype.EXTENSION_FOLDER = "extension";
CSInterface.prototype.APPLICATION = "application";
CSInterface.prototype.USER_DATA = "userData";
CSInterface.prototype.COMMON_FILES = "commonFiles";
CSInterface.prototype.MY_DOCUMENTS = "myDocuments";
CSInterface.prototype.HOST_APPLICATION = "hostApplication";
